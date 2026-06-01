#!/usr/bin/env python3
"""Build and run UDP message tests from source protocol XML files."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from create_test_project import (
    build_project,
    default_protocols,
    ensure_protocol,
    load_manifest,
    protocol_fields,
    protocol_map,
    write_json,
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="根据源协议XML构造消息并测试接口8生成工程的UDP收发",
    )
    parser.add_argument(
        "--generated-dir",
        help="接口8生成工程目录；不传时按 --source-xml 和 --source-port 自动扫描",
    )
    parser.add_argument(
        "--generated-root",
        default="/nfs/615/interface_projects/test/output",
        help="自动扫描接口8生成目录的根目录，默认 /nfs/615/interface_projects/test/output",
    )
    source_input = parser.add_mutually_exclusive_group(required=True)
    source_input.add_argument(
        "--source-xml",
        help="源协议XML文件夹；目录内应包含一个协议相关的全部源XML。也兼容单个XML文件",
    )
    source_input.add_argument(
        "--source-dir",
        help="兼容别名：源协议XML文件夹；建议统一使用 --source-xml 传文件夹",
    )
    parser.add_argument(
        "--output-dir",
        help="测试工程输出目录；默认生成在 generated-dir 同级的 generated_project_message_test",
    )
    parser.add_argument(
        "--source-protocol",
        help="源协议 type_name；默认取 manifest 第一条 conversion 的第一个 source.protocol",
    )
    parser.add_argument(
        "--target-protocol",
        help="目标协议 type_name；默认取 manifest 第一条 conversion 的 target_protocol",
    )
    parser.add_argument(
        "--mode",
        choices=("send", "recv", "roundtrip"),
        default="send",
        help="测试模式；send 只发送源消息，roundtrip 会等待目标消息返回",
    )
    parser.add_argument(
        "--source-ip",
        help="发送源协议消息的目标IP；默认使用 manifest 中的 recv_ip",
    )
    parser.add_argument(
        "--source-port",
        "--recv-port",
        type=int,
        help="发送源协议消息的目标端口，也就是接口8生成工程的接收端口",
    )
    parser.add_argument(
        "--target-ip",
        help="roundtrip/recv 监听目标消息的IP；默认使用 manifest 中的 send_ip",
    )
    parser.add_argument(
        "--target-port",
        "--send-port",
        type=int,
        help="roundtrip/recv 监听目标消息的端口，也就是接口8生成工程的发送端口",
    )
    parser.add_argument(
        "--value-mode",
        choices=("random", "default"),
        default="random",
        help="未用 --set 覆盖时的取值方式；random 按位宽生成随机值，default 使用XML defaultValue",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="随机数种子；不传时每次运行生成不同测试值",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_pairs",
        help="覆盖XML默认值，格式 field=value，可重复传入",
    )
    parser.add_argument(
        "--missing-value",
        type=int,
        default=0,
        help="XML未给 defaultValue 时使用的字段值，默认0",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        help="recv/roundtrip 等待目标消息的超时时间",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=max(1, min(4, os.cpu_count() or 1)),
        help="make 并发数",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="只生成测试工程和值文件，不执行 qmake/make",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="只生成并编译测试工程，不运行测试程序",
    )
    return parser.parse_args()


def local_name(tag: str) -> str:
    """Return the XML local tag name without namespace."""
    return str(tag or "").split("}", 1)[-1].split(":", 1)[-1]


def normalize_key(value: Any) -> str:
    """Normalize field labels for XML/manifest matching."""
    return re.sub(r"[\s_\-./:：()（）\[\]【】]+", "", str(value or "").strip().lower())


def parse_int(value: Any) -> int | None:
    """Parse integer-like XML values."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def xml_field_defaults(source_xml: Path) -> dict[str, int]:
    """Extract field defaults from an XML protocol description."""
    if not source_xml.is_file():
        raise FileNotFoundError(f"源协议XML不存在: {source_xml}")
    root = ET.parse(source_xml).getroot()
    defaults: dict[str, int] = {}
    for element in root.iter():
        tag = local_name(element.tag)
        if tag not in {"Item", "NetCtrl", "SpecType", "StructMess"}:
            continue
        name = str(element.attrib.get("name") or "").strip()
        if not name:
            continue
        raw_default = (
            element.attrib.get("defaultValue")
            if element.attrib.get("defaultValue") is not None
            else element.attrib.get("default")
        )
        parsed = parse_int(raw_default)
        if parsed is None:
            parsed = parse_int(element.attrib.get("value"))
        if parsed is None:
            continue
        defaults[name] = parsed
    return defaults


def xml_field_names(source_xml: Path) -> set[str]:
    """Return normalized field names declared in source XML."""
    if not source_xml.is_file():
        raise FileNotFoundError(f"源协议XML不存在: {source_xml}")
    root = ET.parse(source_xml).getroot()
    names: set[str] = set()
    for element in root.iter():
        if local_name(element.tag) not in {"Item", "NetCtrl", "SpecType", "StructMess"}:
            continue
        name = str(element.attrib.get("name") or "").strip()
        if name:
            names.add(normalize_key(name))
    return names


def list_source_xmls(source_dir: Path) -> list[Path]:
    """List XML files in a source protocol directory."""
    if not source_dir.is_dir():
        raise NotADirectoryError(f"源协议XML目录不存在: {source_dir}")
    xmls = sorted(path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() == ".xml")
    if not xmls:
        raise FileNotFoundError(f"源协议XML目录中没有xml文件: {source_dir}")
    return xmls


def combined_xml_field_names(source_paths: list[Path]) -> set[str]:
    """Return normalized field names from all source XML files."""
    names: set[str] = set()
    for path in source_paths:
        names.update(xml_field_names(path))
    return names


def manifest_source_protocol_names(manifest: dict[str, Any]) -> set[str]:
    """Return protocol names used as conversion sources."""
    names: set[str] = set()
    for conversion in manifest.get("conversions") or []:
        for source in conversion.get("sources") or []:
            name = str(source.get("protocol") or "").strip()
            if name:
                names.add(name)
    return names


def source_match_score(protocol: dict[str, Any], xml_names: set[str]) -> int:
    """Score how well a manifest protocol matches a source XML file."""
    score = 0
    for field in protocol_fields(protocol):
        candidates = {
            normalize_key(field.get("label")),
            normalize_key(field.get("path")),
            normalize_key(str(field.get("path") or "").split("/")[-1]),
            normalize_key(field.get("cpp_name")),
        }
        if candidates & xml_names:
            score += 1
    return score


def auto_resolve_generated_dir(
    generated_root: Path,
    source_paths: list[Path],
    source_port: int | None,
) -> Path:
    """Find an interface8 generated directory matching XML fields and port."""
    xml_names = combined_xml_field_names(source_paths)
    candidates: list[tuple[int, Path]] = []
    for manifest_path in generated_root.glob("**/protocol_manifest.json"):
        generated_dir = manifest_path.parent
        if not (generated_dir / "codec.cpp").is_file():
            continue
        try:
            manifest = load_manifest(generated_dir)
            protocols = protocol_map(manifest)
        except Exception:
            continue
        transport = (manifest.get("runtime") or {}).get("transport") or {}
        if source_port is not None and int(transport.get("recv_port") or 0) != int(source_port):
            continue
        best_score = 0
        for protocol_name in manifest_source_protocol_names(manifest):
            protocol = protocols.get(protocol_name)
            if protocol:
                best_score = max(best_score, source_match_score(protocol, xml_names))
        if best_score > 0:
            candidates.append((best_score, generated_dir))

    if not candidates:
        port_text = f" 且接收端口为 {source_port}" if source_port is not None else ""
        raise FileNotFoundError(f"未找到匹配源XML{port_text}的接口8生成目录")

    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    best_score, best_dir = candidates[0]
    tied = [path for score, path in candidates if score == best_score]
    if len(tied) > 1:
        options = "\n".join(str(path) for path in tied[:8])
        raise ValueError(f"匹配到多个接口8生成目录，请显式传 --generated-dir:\n{options}")
    return best_dir


def random_field_value(field: dict[str, Any], rng: random.Random) -> int:
    """Generate a legal non-negative integer for a field bit width."""
    bit_length = int(field.get("bit_length") or 0)
    if bit_length <= 0:
        return 0
    capped_bits = min(bit_length, 30)
    upper = (1 << capped_bits) - 1
    return rng.randint(0, upper)


def source_value_payload(
    source_protocol: dict[str, Any],
    source_xml: Path,
    missing_value: int,
    overrides: list[str],
    value_mode: str,
    rng: random.Random,
) -> dict[str, int]:
    """Build cpp-field values from XML defaults/random values and CLI overrides."""
    defaults = xml_field_defaults(source_xml)
    lookup: dict[str, int] = {}
    for name, value in defaults.items():
        lookup[normalize_key(name)] = value

    values: dict[str, int] = {}
    for field in protocol_fields(source_protocol):
        if value_mode == "random":
            values[str(field["cpp_name"])] = random_field_value(field, rng)
        else:
            candidates = [
                field.get("label"),
                field.get("path"),
                str(field.get("path") or "").split("/")[-1],
                field.get("cpp_name"),
            ]
            resolved = None
            for candidate in candidates:
                key = normalize_key(candidate)
                if key in lookup:
                    resolved = lookup[key]
                    break
            values[str(field["cpp_name"])] = missing_value if resolved is None else int(resolved)

    for pair in overrides:
        if "=" not in pair:
            raise ValueError(f"--set 格式错误，应为 field=value: {pair}")
        name, raw_value = pair.split("=", 1)
        parsed = parse_int(raw_value)
        if parsed is None:
            raise ValueError(f"--set 值不是整数: {pair}")
        values[name.strip()] = parsed
    return values


def conversion_source_names(manifest: dict[str, Any], source_override: str | None) -> list[str]:
    """Resolve source protocol names for the first manifest conversion."""
    if source_override:
        return [source_override]
    conversions = manifest.get("conversions") or []
    if not conversions:
        raise ValueError("manifest 中没有 conversions，无法推断源协议")
    sources = conversions[0].get("sources") or []
    names = [str(item.get("protocol") or "").strip() for item in sources]
    names = [name for name in names if name]
    if not names:
        raise ValueError("manifest conversion 中没有 sources")
    return names


def conversion_target_name(manifest: dict[str, Any], target_override: str | None) -> str:
    """Resolve the target protocol name for the first manifest conversion."""
    if target_override:
        return target_override
    conversions = manifest.get("conversions") or []
    if not conversions:
        raise ValueError("manifest 中没有 conversions，无法推断目标协议")
    target = str(conversions[0].get("target_protocol") or "").strip()
    if not target:
        raise ValueError("manifest conversion 中没有 target_protocol")
    return target


def match_source_xmls(
    protocols: dict[str, dict[str, Any]],
    source_names: list[str],
    source_paths: list[Path],
) -> dict[str, Path]:
    """Match each source protocol to one XML file by field names."""
    matched: dict[str, Path] = {}
    used: set[Path] = set()
    xml_name_cache = {path: xml_field_names(path) for path in source_paths}
    for source_name in source_names:
        protocol = ensure_protocol(protocols, source_name)
        scored: list[tuple[int, Path]] = []
        for path, names in xml_name_cache.items():
            if path in used:
                continue
            scored.append((source_match_score(protocol, names), path))
        scored.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
        if not scored or scored[0][0] <= 0:
            raise ValueError(f"未在源XML目录中找到匹配协议 {source_name} 的XML")
        matched[source_name] = scored[0][1]
        used.add(scored[0][1])
    return matched


def run_command(command: list[str], cwd: Path) -> None:
    """Run a subprocess and stream output."""
    print(f"+ {' '.join(command)}")
    subprocess.run(command, cwd=str(cwd), check=True)


def resolve_output_dir(generated_dir: Path, output_dir: str | None) -> Path:
    """Resolve the generated test-project directory."""
    if output_dir:
        return Path(output_dir).resolve()
    return generated_dir.parent / f"{generated_dir.name}_message_test"


def build_values_file(
    source_protocol: dict[str, Any],
    source_xml: Path,
    output_dir: Path,
    missing_value: int,
    overrides: list[str],
    value_mode: str,
    rng: random.Random,
) -> Path:
    """Create source_xml_values.json for one source protocol."""
    values = source_value_payload(
        source_protocol,
        source_xml,
        missing_value,
        overrides,
        value_mode,
        rng,
    )
    values_path = output_dir / "source_xml_values.json"
    write_json(values_path, values)
    return values_path


def build_test_binary(
    generated_dir: Path,
    output_dir: Path,
    source_type: str,
    target_type: str,
    jobs: int,
    no_build: bool,
) -> Path:
    """Generate and optionally compile one test binary."""
    build_project(generated_dir, output_dir, source_type, target_type)
    if no_build:
        return output_dir / "build" / "xml_message_test"
    build_dir = output_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    run_command(["qmake", "../xml_message_test.pro"], build_dir)
    run_command(["make", f"-j{max(1, int(jobs))}"], build_dir)
    return build_dir / "xml_message_test"


def run_test_binary(
    binary: Path,
    values_path: Path,
    mode: str,
    args: argparse.Namespace,
) -> None:
    """Run one generated xml_message_test binary."""
    command = [
        str(binary),
        "--mode",
        mode,
        "--values-json",
        str(values_path),
        "--timeout-ms",
        str(args.timeout_ms),
    ]
    if args.source_ip:
        command.extend(["--source-ip", args.source_ip])
    if args.source_port is not None:
        command.extend(["--source-port", str(args.source_port)])
    if args.target_ip:
        command.extend(["--target-ip", args.target_ip])
    if args.target_port is not None:
        command.extend(["--target-port", str(args.target_port)])
    run_command(command, binary.parent)


def run_single_source(
    args: argparse.Namespace,
    generated_dir: Path,
    source_xml: Path,
    output_dir: Path,
    source_type: str,
    target_type: str,
    source_protocol: dict[str, Any],
    rng: random.Random,
) -> None:
    """Build and run a test for one source XML."""
    binary = build_test_binary(generated_dir, output_dir, source_type, target_type, args.jobs, args.no_build)
    values_path = build_values_file(
        source_protocol,
        source_xml,
        output_dir,
        args.missing_value,
        args.set_pairs,
        args.value_mode,
        rng,
    )
    print(f"created test project: {output_dir}")
    print(f"created source XML values: {values_path}")
    if not args.no_build and not args.no_run:
        run_test_binary(binary, values_path, args.mode, args)


def run_source_directory(
    args: argparse.Namespace,
    generated_dir: Path,
    source_paths: list[Path],
    output_dir: Path,
    manifest: dict[str, Any],
    rng: random.Random,
) -> None:
    """Build and run tests for all source XMLs in a directory."""
    protocols = protocol_map(manifest)
    source_names = conversion_source_names(manifest, args.source_protocol)
    target_type = conversion_target_name(manifest, args.target_protocol)
    matched_xmls = match_source_xmls(protocols, source_names, source_paths)
    print("matched source XMLs:")
    for source_name in source_names:
        print(f"  {source_name}: {matched_xmls[source_name]}")

    run_order = list(reversed(source_names))
    for index, source_type in enumerate(run_order):
        source_output_dir = output_dir / source_type
        source_protocol = ensure_protocol(protocols, source_type)
        binary = build_test_binary(
            generated_dir,
            source_output_dir,
            source_type,
            target_type,
            args.jobs,
            args.no_build,
        )
        values_path = build_values_file(
            source_protocol,
            matched_xmls[source_type],
            source_output_dir,
            args.missing_value,
            args.set_pairs if len(source_names) == 1 else [],
            args.value_mode,
            rng,
        )
        print(f"created test project: {source_output_dir}")
        print(f"created source XML values: {values_path}")
        if args.no_build or args.no_run:
            continue
        mode = args.mode
        if args.mode == "roundtrip" and index < len(run_order) - 1:
            mode = "send"
        run_test_binary(binary, values_path, mode, args)


def main() -> int:
    """Generate values from XML, build the test app, and run it."""
    args = parse_args()
    source_input_path = Path(args.source_xml or args.source_dir).resolve()
    source_is_dir = source_input_path.is_dir()
    source_paths = list_source_xmls(source_input_path) if source_is_dir else [source_input_path]
    if args.generated_dir:
        generated_dir = Path(args.generated_dir).resolve()
    else:
        generated_dir = auto_resolve_generated_dir(
            Path(args.generated_root).resolve(),
            source_paths,
            args.source_port,
        )
    output_dir = resolve_output_dir(generated_dir, args.output_dir).resolve()

    manifest = load_manifest(generated_dir)
    rng = random.Random(args.seed)
    if source_is_dir:
        run_source_directory(args, generated_dir, source_paths, output_dir, manifest, rng)
    else:
        source_type, target_type = default_protocols(
            manifest,
            args.source_protocol,
            args.target_protocol,
        )
        source_protocol = ensure_protocol(protocol_map(manifest), source_type)
        run_single_source(
            args,
            generated_dir,
            source_paths[0],
            output_dir,
            source_type,
            target_type,
            source_protocol,
            rng,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
