"""Contains code for parsing the IDL file."""

from typing import Sequence, TypedDict
import json
import re

import solders.pubkey
from anchorpy_core.idl import (
    Idl,
    IdlInstruction,
    IdlAccount,
    IdlField,
    IdlType,
    IdlTypeDefined,
    IdlTypeSimple,
    IdlTypeOption,
    IdlTypeDefinition,
    IdlTypeDefinitionTy,
    IdlTypeDefinitionTyStruct,
    IdlTypeArray,
    IdlTypeDefinitionTyEnum,
    IdlEnumVariant,
    IdlTypeVec,
    EnumFields,
    EnumFieldsNamed,
    EnumFieldsTuple,
    IdlErrorCode,
    IdlTypeGenericLenArray,
)

from borsh_construct import U8, CStruct, Vec

from anchorpy.borsh_extension import BorshPubkey


def _idl_address(program_id: solders.pubkey.Pubkey) -> solders.pubkey.Pubkey:
    """Deterministic IDL address as a function of the program id.

    Args:
        program_id: The program ID.

    Returns:
        The public key of the IDL.
    """
    base = solders.pubkey.Pubkey.find_program_address([], program_id)[0]
    return solders.pubkey.Pubkey.create_with_seed(base, "anchor:idl", program_id)


class IdlProgramAccount(TypedDict):
    """The on-chain account of the IDL."""

    authority: solders.pubkey.Pubkey
    data: bytes


IDL_ACCOUNT_LAYOUT = CStruct("authority" / BorshPubkey, "data" / Vec(U8))


def _decode_idl_account(data: bytes) -> IdlProgramAccount:
    """Decode on-chain IDL.

    Args:
        data: binary data from the account that stores the IDL.

    Returns:
        Decoded IDL.
    """
    return IDL_ACCOUNT_LAYOUT.parse(data)


TypeDefs = Sequence[IdlTypeDefinition]


def _fix_error_msg(raw: str) -> str:
    """Fix json IDL multiline error message

    Args:
        raw: json string of the IDL content

    Returns:
        Fixed version
    """
    json_idl = json.loads(raw)

    for err in json_idl["errors"]:
        match = re.search("\n\s*", err["msg"])
        if match is not None:
            err["msg"] = err["msg"].replace(match.group(0), " ")

    return json.dumps(json_idl, indent=2)


def _fix_instructions_discriminants(raw: str) -> str:
    """Fix json IDL instruction discriminant for non anchor contract

    Args:
        raw: json string of the IDL content

    Returns:
        Fixed version
    """
    json_idl = json.loads(raw)

    index = 0
    sub_index = 0
    parent_extension = ""
    for ix in json_idl["instructions"]:
        match = re.match(r"^(?P<ext>.+Extension)(?P<name>.+)", ix["name"])
        if match is not None:
            if parent_extension != "" and parent_extension != match.group("ext"):
                # different ext from prev inst
                index += 1
                sub_index = 0
            parent_extension = match.group("ext")
            ix["discriminant"] = {
                "type": "u16",
                "value": int.from_bytes(
                    int.to_bytes(index, 1, "big") + int.to_bytes(sub_index, 1, "big"),
                    "big",
                ),
            }
            print(
                f"index: {index}, sub: {sub_index}, value: {ix['discriminant']['value']}"
            )
            sub_index += 1
        else:
            if parent_extension != "":
                index += 1
                parent_extension = ""
                sub_index = 0
            ix["discriminant"] = {"type": "u8", "value": index}
            index += 1

    return json.dumps(json_idl, indent=2)


def _load_instructions_discriminants(raw: str) -> {}:
    """Load instructions discriminants if given

    Args:
        raw: json string of the IDL content

    Returns:
        map of instructions name to each discriminant value
    """
    json_idl = json.loads(raw)
    discriminants = {}

    for ix in json_idl["instructions"]:
        # handle non anchor discriminant
        if "discriminant" in ix:
            discriminants[ix["name"]] = hex(ix["discriminant"]["value"])

    return discriminants


def _from_json(raw: str) -> Idl:
    """Load json IDL for non anchor contract

    Args:
        raw: json string of the IDL content

    Returns:
        IDL
    """
    json_idl = json.loads(raw)
    return Idl(
        json_idl["version"],
        json_idl["name"],
        [],
        [],
        _resolve_instructions(json_idl),
        _resolve_accounts(json_idl),
        _resolve_types(json_idl),
        [],
        _resolve_errors(json_idl),
        metadata=json_idl["metadata"],
    )


def _resolve_instructions(json_idl: {}) -> Sequence[IdlTypeDefinition]:
    instructions = []

    for ix in json_idl["instructions"]:
        accounts = []
        for acc in ix["accounts"]:
            accounts.append(
                IdlAccount(
                    acc["name"], acc["isMut"], acc["isSigner"], None, None, None, []
                )
            )

        args = []
        if "args" in ix:
            for arg in ix["args"]:
                if not isinstance(arg["type"], str) and "coption" in arg["type"]:
                    if "prefix" in arg["type"]:
                        args.append(
                            IdlField(
                                arg["name"] + "_prefix",
                                None,
                                _resolve_idl_type_simple(arg["type"]["prefix"]),
                            )
                        )

                    args.append(
                        IdlField(
                            arg["name"],
                            None,
                            _resolve_idl_type({"type": arg["type"]["coption"]}),
                        )
                    )
                else:
                    args.append(IdlField(arg["name"], None, _resolve_idl_type(arg)))
        instructions.append(IdlInstruction(ix["name"], None, accounts, args, None))

    return instructions


def _resolve_accounts(json_idl: {}) -> Sequence[IdlTypeDefinition]:
    accounts = []
    for acc in json_idl["accounts"]:
        if acc["type"]["kind"] == "struct":
            fields = []
            for f in acc["type"]["fields"]:
                if not isinstance(f["type"], str) and "coption" in f["type"]:
                    if "prefix" in f["type"]:
                        fields.append(
                            IdlField(
                                f["name"] + "_prefix",
                                None,
                                _resolve_idl_type_simple(f["type"]["prefix"]),
                            )
                        )

                    fields.append(
                        IdlField(
                            f["name"],
                            None,
                            _resolve_idl_type({"type": f["type"]["coption"]}),
                        )
                    )
                else:
                    fields.append(IdlField(f["name"], None, _resolve_idl_type(f)))

            accounts.append(
                IdlTypeDefinition(acc["name"], None, IdlTypeDefinitionTyStruct(fields))
            )
        else:
            print(f"unhandled account kind {acc['type']['kind']}")

    return accounts


def _resolve_types(json_idl: {}) -> Sequence[IdlTypeDefinition]:
    types = []
    for t in json_idl["types"]:
        ty: IdlTypeDefinitionTy
        if t["type"]["kind"] == "enum":
            variants = []
            for v in t["type"]["variants"]:
                if "fields" in v:
                    fields_named = []
                    fields_tuple = []

                    for f in v["fields"]:
                        if "name" in f:
                            fields_named.append(
                                IdlField(f["name"], None, _resolve_idl_type(f))
                            )
                        else:
                            fields_tuple.append(_resolve_idl_type({"type": f}))

                    if len(fields_named) > 0:
                        variants.append(
                            IdlEnumVariant(v["name"], EnumFieldsNamed(fields_named))
                        )
                    elif len(fields_tuple) > 0:
                        variants.append(
                            IdlEnumVariant(v["name"], EnumFieldsTuple(fields_tuple))
                        )
                else:
                    variants.append(IdlEnumVariant(v["name"]))

            ty = IdlTypeDefinitionTyEnum(variants)
        elif t["type"]["kind"] == "struct":
            fields_named = []
            for f in t["type"]["fields"]:
                fields_named.append(IdlField(f["name"], None, _resolve_idl_type(f)))

            ty = IdlTypeDefinitionTyStruct(fields_named)
        else:
            print(f"unhandled type kind {t['type']['kind']}")

        types.append(IdlTypeDefinition(t["name"], None, ty))

    return types


def _resolve_errors(json_idl: {}) -> Sequence[IdlErrorCode]:
    errors = []
    if "errors" not in json_idl:
        return errors

    for error in json_idl["errors"]:
        errors.append(IdlErrorCode(error["code"], error["name"], error["msg"]))

    return errors


def _resolve_idl_type(input) -> IdlType:
    ty: IdlType
    if isinstance(input["type"], str):
        ty = _resolve_idl_type_simple(input["type"])
    elif "array" in input["type"]:
        ty = IdlTypeArray(
            (
                _resolve_idl_type_simple(input["type"]["array"][0]),
                input["type"]["array"][1],
            )
        )
    elif "vec" in input["type"]:
        # wrap the vector to allow recursive resolve
        ty = IdlTypeVec(_resolve_idl_type({"type": input["type"]["vec"]}))
    elif "defined" in input["type"]:
        ty = IdlTypeDefined(input["type"]["defined"])
    elif "option" in input["type"]:
        ty = IdlTypeOption(_resolve_idl_type({"type": input["type"]["option"]}))
    elif "hashMap" in input["type"]:
        ty = IdlTypeGenericLenArray(
            (_resolve_idl_type({"type": input["type"]["hashMap"][1]}), "hashMap")
        )
    else:
        print(f"unhandle idl type: {input['type']}")
        # hack to avoid crashing
        return IdlTypeSimple.String

    return ty


def _resolve_idl_type_simple(ty_str: str) -> IdlType:
    ty: IdlType
    if ty_str == "u8":
        ty = IdlTypeSimple.U8
    elif ty_str == "i8":
        ty = IdlTypeSimple.I8
    elif ty_str == "u16":
        ty = IdlTypeSimple.U16
    elif ty_str == "i16":
        ty = IdlTypeSimple.I16
    elif ty_str == "u32":
        ty = IdlTypeSimple.U32
    elif ty_str == "i32":
        ty = IdlTypeSimple.I32
    elif ty_str == "u64":
        ty = IdlTypeSimple.U64
    elif ty_str == "i64":
        ty = IdlTypeSimple.I64
    elif ty_str == "publicKey":
        ty = IdlTypeSimple.PublicKey
    elif ty_str == "string":
        ty = IdlTypeSimple.String
    elif ty_str == "bool":
        ty = IdlTypeSimple.Bool
    elif ty_str == "bytes":
        ty = IdlTypeSimple.Bytes
    else:
        print(f"unhandle idl type simple: {ty_str}")

    return ty
