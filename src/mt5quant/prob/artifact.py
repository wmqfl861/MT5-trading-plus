"""模型产物（T72，蓝图 P 线附录 B / 决策 D43）：冻结为带哈希的 JSON 文件。

哈希按规范化 JSON（键排序、UTF-8、ensure_ascii=False、无多余空白）计算；
文件内容（原始字节）与传入 expected_sha256 不符一律拒绝加载
（ModelIntegrityError，fail-closed）。写入走仓库统一原子写模式（铁律 15，
与 mt5gw.snapshot.write_json_atomic 同一形态：唯一临时文件 pid+uuid、
fsync 落盘、os.replace 带重试、失败清理自己的 tmp）。NaN/Inf 拒绝序列化
（allow_nan=False）。评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path


class ModelIntegrityError(Exception):
    """模型文件缺失或哈希不符（fail-closed）。"""


def _canonical_bytes(model: dict) -> bytes:
    return json.dumps(model, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """铁律 15 统一原子写（与 mt5gw.snapshot 同一模式；本包不 import 实盘
    链路模块）：唯一 tmp（pid+uuid）→ fsync → os.replace 带重试（Windows
    读者占句柄时，总预算约 3 秒）；替换失败清理自己的 tmp 再上抛。"""
    tmp = path.parent / f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_bytes(data)
    fd = os.open(str(tmp), os.O_RDWR)   # Windows：fsync 需写句柄
    try:
        os.fsync(fd)                    # 落盘后再替换，掉电不至半截
    finally:
        os.close(fd)
    try:
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.3)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_model(model: dict, root) -> tuple[str, str]:
    """把 model 规范化写入 root/<sha256>.json，返回 (路径, sha256)。

    同一输入两次生成的字节与哈希相同；重复保存同一模型为幂等覆盖。
    """
    data = _canonical_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest}.json"
    _atomic_write_bytes(path, data)
    return str(path), digest


def load_model(path, *, expected_sha256: str) -> dict:
    """读取并校验模型文件；哈希不符或文件缺失 → ModelIntegrityError。"""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise ModelIntegrityError(f"模型文件不可读：{p.name}（{e}）") from e
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ModelIntegrityError(
            f"模型文件 {p.name} 哈希与预期不符（fail-closed）")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ModelIntegrityError(f"模型文件 {p.name} 不是合法 JSON：{e}") from e
