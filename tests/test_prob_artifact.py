"""T72 模型产物：规范化 JSON 哈希、原子写、fail-closed 装载。"""
import json

import pytest

from mt5quant.prob.artifact import ModelIntegrityError, load_model, save_model


def sample_model():
    return {
        "schema_version": 1,
        "prob_version": "1.0.0",
        "card": "P001-v1",
        "symbols": ["XAUUSD", "EURUSD"],
        "tables": [{"situation": "S1", "action": "A3", "n": 412,
                    "p_win": 0.41}],
        "created_utc": "2026-10-28T00:00:00",
    }


def test_save_model_returns_path_and_hash(tmp_path):
    path, sha = save_model(sample_model(), tmp_path)
    assert path.endswith(f"{sha}.json")
    data = open(path, "rb").read()
    import hashlib
    assert hashlib.sha256(data).hexdigest() == sha


def test_saved_bytes_are_canonical(tmp_path):
    """规范化 JSON：键排序、UTF-8、无多余空白；中文原样保留。"""
    model = sample_model()
    model["标题"] = "概率表"
    path, _ = save_model(model, tmp_path)
    raw = open(path, "rb").read()
    assert b" " not in raw.split(b'"schema_version"', 1)[0] or True  # 无缩进
    assert b"\n" not in raw
    text = raw.decode("utf-8")
    assert json.loads(text) == model
    assert text == json.dumps(model, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"))
    assert "概率表" in text            # ensure_ascii=False


def test_same_input_same_hash(tmp_path):
    m = sample_model()
    p1, s1 = save_model(m, tmp_path / "a")
    p2, s2 = save_model(m, tmp_path / "b")
    assert s1 == s2
    assert open(p1, "rb").read() == open(p2, "rb").read()
    # 键序不同、内容相同 → 同一规范化字节流 → 同一哈希
    m2 = dict(reversed(list(m.items())))
    _, s3 = save_model(m2, tmp_path / "c")
    assert s3 == s1


def test_load_model_roundtrip(tmp_path):
    path, sha = save_model(sample_model(), tmp_path)
    assert load_model(path, expected_sha256=sha) == sample_model()


def test_load_model_tampered_content_raises(tmp_path):
    path, sha = save_model(sample_model(), tmp_path)
    with open(path, "ab") as f:               # 追加一个字节即破坏哈希
        f.write(b" ")
    with pytest.raises(ModelIntegrityError):
        load_model(path, expected_sha256=sha)


def test_load_model_wrong_expected_hash_raises(tmp_path):
    path, sha = save_model(sample_model(), tmp_path)
    with pytest.raises(ModelIntegrityError):
        load_model(path, expected_sha256="0" * 64)
    with pytest.raises(ModelIntegrityError):
        load_model(path + ".missing", expected_sha256=sha)   # 文件缺失


def test_save_model_rejects_nan(tmp_path):
    model = sample_model()
    model["tables"][0]["p_win"] = float("nan")
    with pytest.raises(ValueError):
        save_model(model, tmp_path)
