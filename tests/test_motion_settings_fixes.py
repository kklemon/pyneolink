"""Regression tests for motion/battery lease handling and IR/PIR set replies."""

import gc
import time

import pytest

from pyneolink import EVENTS
from pyneolink.battery import BatteryInfoUpdates
from pyneolink.core.bc import Header, Message, ProtocolError, xml_document
from pyneolink.core.const import MSG, MSG_CLASS
from pyneolink.internal.camera import CameraOnlineLease
from pyneolink.motion import CameraEvent, CameraEvents
from pyneolink.settings import Ir, Pir


def motion_reply(ai_type="people", status="MD"):
    xml = xml_document(
        '<AlarmEventList version="1.1">'
        f'<AlarmEvent version="1.1"><channelId>0</channelId><status>{status}</status><AItype>{ai_type}</AItype></AlarmEvent>'
        "</AlarmEventList>"
    )
    return Message(Header(MSG.MOTION, len(xml), 0, 0, 2, 200, MSG_CLASS.MODERN), payload=xml)


class FakeCamera:
    """Fake camera that tracks the online-lease counter and recv timeouts."""

    def __init__(self, *, replies=None, command_error=None, command_response_code=200, send_sleep=0.0, timeout=0.05):
        self.config = type("Config", (), {"channel_id": 0})()
        self.timeout = timeout
        self._online_required = 0
        self.replies = list(replies or [])
        self.command_error = command_error
        self.command_response_code = command_response_code
        self.commands = []
        self.keepalives = 0
        self.recv_timeouts = []
        self.send_sleep = send_sleep

    def require_online(self):
        return CameraOnlineLease(self)

    def command(self, msg_id, payload=b"", *, extension=b""):
        self.commands.append(msg_id)
        if self.command_error is not None:
            raise self.command_error
        return Message(Header(msg_id, 0, 0, 0, 1, self.command_response_code, MSG_CLASS.MODERN), payload=b"")

    def send(self, msg_id, payload=b"", *, extension=b"", **_kwargs):
        self.keepalives += 1
        if self.send_sleep:
            time.sleep(self.send_sleep)
        return 1

    def _recv(self, timeout=None):
        self.recv_timeouts.append(timeout)
        assert timeout is None or timeout > 0, f"_recv called with non-positive timeout {timeout!r}"
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        raise TimeoutError("no reply")


class FakeBattery:
    def __init__(self, camera):
        self.camera = camera
        self.refreshes = 0

    def refresh(self, mode="reconnect"):
        self.refreshes += 1
        return {"level_percent": 50}

    def keepalive(self):
        pass


# FIX 1: online lease must be released when start()/status() fail.


@pytest.mark.parametrize("error", [TimeoutError("flaky link"), EOFError("closed"), OSError("socket died")])
def test_start_releases_lease_when_motion_request_raises(error):
    camera = FakeCamera(command_error=error)
    events = CameraEvents(camera)

    with pytest.raises(type(error)):
        events.start()

    assert camera._online_required == 0


def test_start_releases_lease_on_rejected_motion_request():
    camera = FakeCamera(command_response_code=400)
    events = CameraEvents(camera)

    with pytest.raises(ProtocolError):
        events.start()

    assert camera._online_required == 0


def test_status_releases_lease_when_start_fails():
    camera = FakeCamera(command_error=EOFError("link dropped"))
    events = CameraEvents(camera)

    with pytest.raises(EOFError):
        events.status(timeout=0.1)

    assert camera._online_required == 0
    with pytest.raises(StopIteration):
        next(events)


def test_status_releases_lease_when_recv_fails_mid_wait():
    camera = FakeCamera(replies=[OSError("socket died")])
    events = CameraEvents(camera)

    with pytest.raises(OSError):
        events.status(timeout=0.5)

    assert camera._online_required == 0


# FIX 2: never call _recv with a non-positive timeout after the deadline
# passed between the top-of-loop check and the recv call.


def test_next_skips_recv_when_deadline_expires_during_keepalive():
    camera = FakeCamera(send_sleep=0.05)
    events = CameraEvents(camera, duration=0.02, keepalive_interval=0.0)

    with pytest.raises(StopIteration):
        next(events)

    assert camera.keepalives == 1
    assert camera.recv_timeouts == []
    assert camera._online_required == 0


def test_status_skips_recv_when_deadline_expires_during_keepalive():
    camera = FakeCamera(send_sleep=0.05)
    events = CameraEvents(camera, keepalive_interval=0.0)

    event, known = events.status(timeout=0.02)

    assert known is False
    assert event.type == EVENTS.none
    assert camera.recv_timeouts == []
    assert camera._online_required == 0


def test_watch_recv_timeouts_stay_positive_until_duration_expires():
    camera = FakeCamera()
    events = CameraEvents(camera, duration=0.02)

    with pytest.raises(StopIteration):
        next(events)

    # The FakeCamera asserts timeout > 0 on every call; make sure the loop
    # actually exercised _recv while the deadline was approaching.
    assert camera.recv_timeouts
    assert all(timeout > 0 for timeout in camera.recv_timeouts)


# FIX 3: a lost IR/PIR SET reply must raise instead of reporting success.


def test_ir_set_without_reply_raises_timeout():
    camera = FakeCamera(timeout=0.05)

    with pytest.raises(TimeoutError, match="IR light set"):
        Ir(camera)._wait_for_set_reply(3)

    assert camera.recv_timeouts
    assert all(timeout > 0 for timeout in camera.recv_timeouts)


def test_pir_set_without_reply_raises_timeout():
    camera = FakeCamera(timeout=0.05)

    with pytest.raises(TimeoutError, match="PIR set"):
        Pir(camera)._wait_for_set_reply(9)


def test_pir_on_with_lost_set_reply_raises_timeout():
    class FakePirCamera:
        config = type("Config", (), {"channel_id": 0})()
        timeout = 0.05

        def __init__(self):
            self.recv_timeouts = []

        def command(self, msg_id, payload=b"", *, extension=b""):
            xml = xml_document('<rfAlarmCfg version="1.1"><rfID>0</rfID><enable>0</enable></rfAlarmCfg>')
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

        def send(self, msg_id, payload=b"", *, extension=b"", **_kwargs):
            return 9

        def _recv(self, timeout=None):
            self.recv_timeouts.append(timeout)
            assert timeout is not None and timeout > 0
            raise TimeoutError("lost reply")

    camera = FakePirCamera()

    with pytest.raises(TimeoutError, match="PIR set"):
        Pir(camera).on()

    assert camera.recv_timeouts


def test_ir_set_rejection_still_raises_protocol_error():
    reply = Message(Header(MSG.SET_LED, 0, 0, 0, 3, 400, MSG_CLASS.MODERN), payload=b"")
    camera = FakeCamera(timeout=1.0, replies=[reply])

    with pytest.raises(ProtocolError, match="400"):
        Ir(camera)._wait_for_set_reply(3)


def test_pir_set_rejection_still_raises_protocol_error():
    reply = Message(Header(MSG.SET_PIR_ALARM, 0, 0, 0, 9, 400, MSG_CLASS.MODERN), payload=b"")
    camera = FakeCamera(timeout=1.0, replies=[reply])

    with pytest.raises(ProtocolError, match="400"):
        Pir(camera)._wait_for_set_reply(9)


# FIX 5: closed/exhausted CameraEvents must stay exhausted.


def test_closed_camera_events_stays_exhausted():
    camera = FakeCamera(replies=[motion_reply()])
    events = CameraEvents(camera)

    assert next(events) == EVENTS.human
    assert camera._online_required == 1

    events.close()
    assert camera._online_required == 0

    with pytest.raises(StopIteration):
        next(events)
    with pytest.raises(StopIteration):
        next(events)
    assert camera.commands == [MSG.MOTION_REQUEST]
    assert camera._online_required == 0

    events.close()  # idempotent
    assert camera._online_required == 0

    with pytest.raises(ProtocolError, match="closed"):
        events.start()
    assert camera._online_required == 0


def test_duration_exhausted_camera_events_stays_exhausted():
    camera = FakeCamera()
    events = CameraEvents(camera, duration=0.01)

    with pytest.raises(StopIteration):
        next(events)
    with pytest.raises(StopIteration):
        next(events)

    assert camera.commands == [MSG.MOTION_REQUEST]
    assert camera._online_required == 0


def test_iterating_closed_camera_events_yields_nothing():
    camera = FakeCamera()
    events = CameraEvents(camera, duration=0.01)
    events.close()

    assert list(events) == []
    assert camera.commands == []
    assert camera._online_required == 0


# FIX 4: abandoned iterators release the lease; context managers still work.


def test_abandoned_watch_iterator_releases_lease_on_gc():
    camera = FakeCamera(replies=[motion_reply()])
    events = CameraEvents(camera)
    for _event in events:
        break
    assert camera._online_required == 1

    del events
    gc.collect()

    assert camera._online_required == 0


def test_camera_events_context_manager_releases_lease():
    camera = FakeCamera(replies=[motion_reply()])
    with CameraEvents(camera) as events:
        assert camera._online_required == 1
        assert next(events) == EVENTS.human
    assert camera._online_required == 0


def test_battery_updates_close_releases_lease_and_is_idempotent():
    camera = FakeCamera()
    updates = BatteryInfoUpdates(FakeBattery(camera), interval=0.001, count=2, mode="online")

    assert next(updates)["level_percent"] == 50
    assert camera._online_required == 1

    updates.close()
    assert camera._online_required == 0
    updates.close()
    assert camera._online_required == 0

    with pytest.raises(StopIteration):
        next(updates)


def test_battery_updates_context_manager_releases_lease():
    camera = FakeCamera()
    with BatteryInfoUpdates(FakeBattery(camera), interval=0.0, count=1, mode="online") as updates:
        assert camera._online_required == 1
        assert next(updates)["level_percent"] == 50
    assert camera._online_required == 0


def test_abandoned_battery_updates_releases_lease_on_gc():
    camera = FakeCamera()
    updates = BatteryInfoUpdates(FakeBattery(camera), interval=0.001, count=5, mode="online")
    next(updates)
    assert camera._online_required == 1

    del updates
    gc.collect()

    assert camera._online_required == 0


# FIX 6: hash must be consistent with the EVENTS-aware equality.


def test_camera_event_hash_matches_event_equality():
    event = CameraEvent(EVENTS.human, active=True)

    assert event == EVENTS.human
    assert hash(event) == hash(EVENTS.human)
    assert event in {EVENTS.human, EVENTS.vehicle}
    assert EVENTS.human in {event}
    assert event not in {EVENTS.vehicle}
    assert {EVENTS.human: "seen"}[event] == "seen"


def test_camera_event_hash_differs_by_type_not_fields():
    active = CameraEvent(EVENTS.vehicle, active=True)
    inactive = CameraEvent(EVENTS.vehicle, active=False, channel_id=3)

    assert hash(active) == hash(inactive) == hash(EVENTS.vehicle)
    assert active in {EVENTS.vehicle}
    assert inactive in {EVENTS.vehicle}
