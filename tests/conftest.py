import sys
import types

import pytest
from google.protobuf import descriptor_pb2
from google.protobuf import descriptor_pool
from google.protobuf import message_factory
from google.protobuf import timestamp_pb2  # noqa: F401 - ensures Timestamp descriptor is loaded

from proto_converter import converter as converter_module
from proto_converter import factory as factory_module
from proto_converter.factory import clear_converter_cache
from proto_converter.transforms import TRANSFORMS

_TEST_MODULE_NAME = "proto_converter_test_pb2"
_ORIGINAL_TRANSFORMS = dict(TRANSFORMS)


def _add_scalar_field(
    message: descriptor_pb2.DescriptorProto,
    name: str,
    number: int,
    field_type: int,
) -> None:
    field = message.field.add()
    field.name = name
    field.number = number
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = field_type


def _build_test_module() -> types.ModuleType:
    if _TEST_MODULE_NAME in sys.modules:
        return sys.modules[_TEST_MODULE_NAME]

    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "proto_converter_test.proto"
    file_proto.package = "proto_converter.test"
    file_proto.syntax = "proto3"
    file_proto.dependency.append("google/protobuf/timestamp.proto")

    status_enum = file_proto.enum_type.add()
    status_enum.name = "Status"
    for enum_name, enum_number in (
        ("STATUS_UNSPECIFIED", 0),
        ("STATUS_ACTIVE", 1),
        ("STATUS_INACTIVE", 2),
    ):
        value = status_enum.value.add()
        value.name = enum_name
        value.number = enum_number

    date_like = file_proto.message_type.add()
    date_like.name = "DateLike"
    _add_scalar_field(date_like, "year", 1, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)
    _add_scalar_field(date_like, "month", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)
    _add_scalar_field(date_like, "day", 3, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)

    child = file_proto.message_type.add()
    child.name = "Child"
    _add_scalar_field(child, "note", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)

    sample = file_proto.message_type.add()
    sample.name = "Sample"
    _add_scalar_field(sample, "name", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    _add_scalar_field(sample, "count", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)

    status_field = sample.field.add()
    status_field.name = "status"
    status_field.number = 3
    status_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    status_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
    status_field.type_name = ".proto_converter.test.Status"

    created_at_field = sample.field.add()
    created_at_field.name = "created_at"
    created_at_field.number = 4
    created_at_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    created_at_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    created_at_field.type_name = ".google.protobuf.Timestamp"

    tags_field = sample.field.add()
    tags_field.name = "tags"
    tags_field.number = 5
    tags_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
    tags_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

    child_field = sample.field.add()
    child_field.name = "child"
    child_field.number = 6
    child_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    child_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    child_field.type_name = ".proto_converter.test.Child"

    date_field = sample.field.add()
    date_field.name = "settlement_date"
    date_field.number = 7
    date_field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    date_field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    date_field.type_name = ".proto_converter.test.DateLike"

    pool = descriptor_pool.Default()
    pool.AddSerializedFile(file_proto.SerializeToString())

    module = types.ModuleType(_TEST_MODULE_NAME)
    module.Sample = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("proto_converter.test.Sample")
    )
    module.Child = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("proto_converter.test.Child")
    )
    module.DateLike = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("proto_converter.test.DateLike")
    )
    module.STATUS_UNSPECIFIED = 0
    module.STATUS_ACTIVE = 1
    module.STATUS_INACTIVE = 2
    sys.modules[_TEST_MODULE_NAME] = module
    return module


@pytest.fixture(scope="session")
def proto_module():
    return _build_test_module()


@pytest.fixture(autouse=True)
def reset_global_state():
    clear_converter_cache()
    factory_module._mapping_dirs.clear()
    converter_module._expression_functions.clear()
    converter_module._expression_names.clear()
    TRANSFORMS.clear()
    TRANSFORMS.update(_ORIGINAL_TRANSFORMS)
    yield
    clear_converter_cache()
    factory_module._mapping_dirs.clear()
    converter_module._expression_functions.clear()
    converter_module._expression_names.clear()
    TRANSFORMS.clear()
    TRANSFORMS.update(_ORIGINAL_TRANSFORMS)
