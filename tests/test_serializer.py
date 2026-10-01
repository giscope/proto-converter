from google.protobuf.timestamp_pb2 import Timestamp

from proto_converter.serializer import ProtoSerializer


def _sample_timestamp() -> Timestamp:
    message = Timestamp()
    message.FromSeconds(1_720_085_400)
    return message


def test_text_mode_round_trip():
    serializer = ProtoSerializer(use_binary=False)
    original = _sample_timestamp()

    payload = serializer.serialize(original)
    recovered = serializer.deserialize(payload, Timestamp)

    assert serializer.content_type == "text/x-protobuf"
    assert recovered == original


def test_binary_mode_round_trip_and_websocket_payload():
    serializer = ProtoSerializer(use_binary=True)
    original = _sample_timestamp()

    payload = serializer.serialize(original)
    recovered = serializer.deserialize(payload, Timestamp)
    ws_payload, is_binary = serializer.for_websocket(original)

    assert serializer.content_type == "application/x-protobuf"
    assert recovered == original
    assert ws_payload == payload
    assert is_binary is True


def test_deserialize_honors_binary_content_type_header():
    serializer = ProtoSerializer(use_binary=False)
    original = _sample_timestamp()
    payload = original.SerializeToString()

    recovered = serializer.deserialize(
        payload,
        Timestamp,
        content_type="application/x-protobuf; charset=binary",
    )
    assert recovered == original
