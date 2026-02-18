"""
Proto Serialization Utility.

Serializes protobuf messages in either text or binary format:
- Text: text_format (human-readable, like .pbtxt) — good for dev/test
- Binary: SerializeToString (compact) — good for production

Usage:
    from proto_converter import ProtoSerializer

    serializer = ProtoSerializer(use_binary=False)

    # Serialize single message
    data = serializer.serialize(message)

    # For HTTP response
    # return HttpResponse(data, content_type=serializer.content_type)

    # For WebSocket
    data, is_binary = serializer.for_websocket(message)
"""

from typing import Type, TypeVar, Union, Tuple

from google.protobuf.message import Message
from google.protobuf import text_format

T = TypeVar('T', bound=Message)


class ProtoSerializer:
    """Protobuf serializer - text format or binary, configurable at init time."""

    def __init__(self, use_binary: bool = False):
        self.use_binary = use_binary

    @property
    def content_type(self) -> str:
        """HTTP Content-Type header value."""
        return "application/x-protobuf" if self.use_binary else "text/x-protobuf"

    def serialize(self, message: Message) -> bytes:
        """Serialize proto message to bytes."""
        if self.use_binary:
            return message.SerializeToString()
        else:
            return text_format.MessageToString(message).encode('utf-8')

    def deserialize(self, data: Union[bytes, str], message_class: Type[T], content_type: str = None) -> T:
        """Deserialize bytes to proto message.

        Args:
            data: Raw bytes or string to deserialize.
            message_class: Target protobuf message class.
            content_type: Optional Content-Type header hint. If 'application/x-protobuf',
                          will parse as binary even in DEV mode.
        """
        message = message_class()

        # Determine if we should parse as binary:
        # 1. If explicitly set to binary mode, or
        # 2. If content_type indicates binary format
        parse_binary = self.use_binary or (
            content_type and 'application/x-protobuf' in content_type
        )

        if parse_binary:
            if isinstance(data, str):
                data = data.encode('latin-1')
            message.ParseFromString(data)
        else:
            if isinstance(data, bytes):
                data = data.decode('utf-8')
            text_format.Parse(data, message)
        return message

    def for_websocket(self, message: Message) -> Tuple[bytes, bool]:
        """
        Prepare message for WebSocket send.
        Returns (data, is_binary).
        """
        data = self.serialize(message)
        return data, self.use_binary
