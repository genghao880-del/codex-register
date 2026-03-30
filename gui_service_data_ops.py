from __future__ import annotations

import glob
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import zipfile
from datetime import datetime
from typing import Any

from gui_config_store import ACCOUNTS_TXT, save_config
from gui_http_utils import _http_post_json


def accounts_txt_path(service) -> str:
    """与 r_with_pwd 写入逻辑一致：有 TOKEN_OUTPUT_DIR 则用其下 accounts.txt。"""
    outdir = os.getenv("TOKEN_OUTPUT_DIR", "").strip()
    if outdir:
        return os.path.join(outdir, ACCOUNTS_TXT)
    return ACCOUNTS_TXT


def emails_from_accounts_json(fp: str) -> set[str]:
    """从导出 JSON 的 accounts 数组收集邮箱，用于删文件时同步 accounts.txt。"""
    emails: set[str] = set()
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        for acc in data.get("accounts", []):
            if not isinstance(acc, dict):
                continue
            e = (
                acc.get("name")
                or (acc.get("credentials") or {}).get("email")
                or (acc.get("extra") or {}).get("email")
            )
            if e and isinstance(e, str):
                emails.add(e.strip())
    except Exception:
        pass
    return emails


def email_from_account_entry(acc: dict[str, Any]) -> str:
    if not isinstance(acc, dict):
        return ""
    e = str(acc.get("name") or "").strip().lower()
    if e:
        return e
    creds = acc.get("credentials") or {}
    if isinstance(creds, dict):
        return str(creds.get("email") or "").strip().lower()
    return ""


def build_local_account_index(service) -> dict[str, dict[str, Any]]:
    """从本地 accounts_*.json 建立 email -> account 字典（新文件优先）。"""
    out: dict[str, dict[str, Any]] = {}
    files = sorted(glob.glob("accounts_*.json"), key=os.path.getmtime, reverse=True)
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                root = json.load(f)
            arr = root.get("accounts", [])
            if not isinstance(arr, list):
                continue
            for acc in arr:
                em = email_from_account_entry(acc)
                if em and em not in out and isinstance(acc, dict):
                    out[em] = acc
        except Exception:
            continue
    return out


def build_email_source_files_map(service) -> dict[str, list[str]]:
    """建立 email -> [来源文件名...] 映射（按文件时间倒序）。"""
    out: dict[str, list[str]] = {}
    files = sorted(glob.glob("accounts_*.json"), key=os.path.getmtime, reverse=True)
    for fp in files:
        name = os.path.basename(fp)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                root = json.load(f)
            arr = root.get("accounts", [])
            if not isinstance(arr, list):
                continue
            for acc in arr:
                if not isinstance(acc, dict):
                    continue
                em = email_from_account_entry(acc)
                if not em:
                    continue
                lst = out.setdefault(em, [])
                if name not in lst:
                    lst.append(name)
        except Exception:
            continue
    return out


def source_label(files: list[str]) -> str:
    if not files:
        return "-"
    if len(files) == 1:
        return files[0]
    return f"{files[0]} +{len(files) - 1}"


def _safe_export_stem(raw: Any, fallback: str) -> str:
    base = str(raw or "").strip()
    if not base:
        base = fallback
    if base.lower().endswith(".json"):
        base = base[:-5]
    base = re.sub(r"[\\/:*?\"<>|]+", "_", base)
    base = re.sub(r"\s+", "_", base)
    base = base.strip("._ ")
    return base or fallback


def _open_directory(path: str) -> bool:
    target = os.path.abspath(str(path or "").strip())
    if not target or not os.path.isdir(target):
        return False
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
            return True
        subprocess.Popen(["xdg-open", target])
        return True
    except Exception:
        return False


def _account_to_codex_record(acc: dict[str, Any]) -> dict[str, str]:
    creds = acc.get("credentials") if isinstance(acc.get("credentials"), dict) else {}
    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}

    email = str(
        acc.get("name")
        or creds.get("email")
        or extra.get("email")
        or ""
    ).strip()
    expired = str(
        creds.get("expires_at")
        or creds.get("expired")
        or acc.get("expired")
        or ""
    ).strip()
    id_token = str(creds.get("id_token") or acc.get("id_token") or "").strip()
    account_id = str(
        creds.get("chatgpt_account_id")
        or creds.get("account_id")
        or acc.get("account_id")
        or ""
    ).strip()
    access_token = str(creds.get("access_token") or acc.get("access_token") or "").strip()
    last_refresh = str(
        creds.get("last_refresh")
        or acc.get("last_refresh")
        or ""
    ).strip()
    refresh_token = str(creds.get("refresh_token") or acc.get("refresh_token") or "").strip()

    return {
        "type": "codex",
        "email": email,
        "expired": expired,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": last_refresh,
        "refresh_token": refresh_token,
    }


def _normalize_remote_account_provider(raw: Any) -> str:
    val = str(raw or "sub2api").strip().lower()
    if val in {"cliproxyapi", "cliproxy", "cli_proxy_api", "cpa"}:
        return "cliproxyapi"
    return "sub2api"


def export_codex_accounts(service, emails: list[Any]) -> dict[str, Any]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        em = str(raw or "").strip().lower()
        if not em or em in seen:
            continue
        seen.add(em)
        ordered.append(em)
    if not ordered:
        raise ValueError("请先勾选账号")

    raw_export_dir = str(service.cfg.get("codex_export_dir") or "").strip()
    if not raw_export_dir:
        raise ValueError("请先设置 CodeX 导出目录")

    export_dir = os.path.abspath(os.path.expanduser(raw_export_dir))
    try:
        os.makedirs(export_dir, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"创建导出目录失败: {e}") from e
    if not os.path.isdir(export_dir):
        raise RuntimeError("CodeX 导出目录不可用")

    local_map = build_local_account_index(service)
    source_map = build_email_source_files_map(service)

    picked: list[tuple[str, dict[str, Any], str]] = []
    missing: list[str] = []
    for em in ordered:
        acc = local_map.get(em)
        if not isinstance(acc, dict):
            missing.append(em)
            continue
        files = list(source_map.get(em, []))
        source_primary = str(files[0] if files else "").strip()
        picked.append((em, acc, source_primary))

    if not picked:
        raise RuntimeError("本地 JSON 中未找到可导出的账号")

    payload_files: list[tuple[str, bytes, str]] = []
    used_names: set[str] = set()
    for idx, (em, acc, src) in enumerate(picked, start=1):
        row = _account_to_codex_record(acc)
        if not row["email"]:
            row["email"] = em

        stem = _safe_export_stem(row["email"], f"account_{idx}")
        filename = f"{stem}.json"
        suffix = 2
        while filename.lower() in used_names:
            filename = f"{stem}_{suffix}.json"
            suffix += 1
        used_names.add(filename.lower())

        body = json.dumps(row, ensure_ascii=False, indent=2).encode("utf-8")
        payload_files.append((filename, body, src))

    if len(payload_files) == 1:
        out_name = payload_files[0][0]
        out_bytes = payload_files[0][1]
    else:
        src_name = ""
        for _, _, src in payload_files:
            if src:
                src_name = src
                break
        zip_stem = _safe_export_stem(src_name, "codex_accounts")

        out_name = f"{zip_stem}.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fn, body, _ in payload_files:
                zf.writestr(fn, body)
        out_bytes = buf.getvalue()

    target_path = os.path.join(export_dir, out_name)
    try:
        with open(target_path, "wb") as f:
            f.write(out_bytes)
    except Exception as e:
        raise RuntimeError(f"写入导出文件失败: {e}") from e

    opened_dir = _open_directory(export_dir)

    service.log(
        f"CodeX 导出完成：选中 {len(ordered)}，导出 {len(payload_files)}"
        + (f"，缺失 {len(missing)}" if missing else "")
        + f"，路径 {target_path}"
    )

    return {
        "filename": out_name,
        "saved_path": target_path,
        "output_dir": export_dir,
        "opened_dir": opened_dir,
        "selected": len(ordered),
        "exported": len(payload_files),
        "missing": missing,
    }


def save_json_file_note(service, path: str, note: str) -> dict[str, Any]:
    target = os.path.abspath(str(path or "").strip())
    if not target:
        raise ValueError("path 不能为空")

    allow = {os.path.abspath(p) for p in glob.glob("accounts_*.json")}
    if target not in allow or not os.path.isfile(target):
        raise ValueError("目标 JSON 文件不存在或不可编辑")

    name = os.path.basename(target)
    clean = str(note or "").strip()
    if len(clean) > 120:
        clean = clean[:120]

    with service._lock:
        notes = service._normalize_json_file_notes(service.cfg.get("json_file_notes") or {})
        if clean:
            notes[name] = clean
        else:
            notes.pop(name, None)
        service.cfg["json_file_notes"] = notes
        save_config(service.cfg)

    service.log(f"已保存备注: {name} -> {clean or '-'}")
    return {
        "path": target,
        "name": name,
        "note": clean,
    }


def list_json_files(service) -> dict[str, Any]:
    with service._lock:
        notes_map = service._normalize_json_file_notes(service.cfg.get("json_file_notes") or {})

    files = sorted(
        glob.glob("accounts_*.json"),
        key=os.path.getmtime,
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    total = 0
    for fp in files:
        fp_abs = os.path.abspath(fp)
        name = os.path.basename(fp_abs)
        try:
            with open(fp_abs, "r", encoding="utf-8") as f:
                data = json.load(f)
            cnt = len(data.get("accounts", []))
        except Exception:
            cnt = 0
        try:
            cdate = datetime.fromtimestamp(os.path.getctime(fp_abs)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            cdate = "-"
        total += cnt
        items.append(
            {
                "path": fp_abs,
                "name": name,
                "count": cnt,
                "created": cdate,
                "note": str(notes_map.get(name) or ""),
                "file_color_idx": service._file_color_index(name),
            }
        )
    return {"items": items, "file_count": len(items), "account_total": total}


def list_accounts(service) -> dict[str, Any]:
    lines: list[str] = []
    ap = accounts_txt_path(service)
    if os.path.exists(ap):
        try:
            with open(ap, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        except Exception:
            lines = []

    local_counts: dict[str, int] = {}
    for line in lines:
        ep = line.split("----", 1)[0].strip().lower()
        if ep:
            local_counts[ep] = local_counts.get(ep, 0) + 1

    email_files_map = build_email_source_files_map(service)
    file_options = [
        os.path.basename(p)
        for p in sorted(glob.glob("accounts_*.json"), key=os.path.getmtime, reverse=True)
    ]

    with service._lock:
        remote_ready = service._remote_sync_status_ready
        remote_counts = dict(service._remote_email_counts)

    items: list[dict[str, Any]] = []
    for i, line in enumerate(lines, start=1):
        parts = line.split("----", 1)
        email = parts[0]
        pwd = parts[1] if len(parts) > 1 else ""
        ep = email.strip().lower()
        status = "normal"
        src_files = list(email_files_map.get(ep, []))
        primary_source = str(src_files[0] if src_files else "")
        if remote_ready:
            remote_cnt = int(remote_counts.get(ep, 0))
            local_cnt = int(local_counts.get(ep, 0))
            if local_cnt > 1 or remote_cnt > 1:
                status = "dup"
            elif remote_cnt > 0:
                status = "ok"
            else:
                status = "pending"
        items.append(
            {
                "key": f"{i}:{email}",
                "index": i,
                "email": email,
                "password": pwd,
                "status": status,
                "source": source_label(src_files),
                "source_files": src_files,
                "source_primary": primary_source,
                "source_color_idx": service._file_color_index(primary_source),
            }
        )
    return {
        "path": ap,
        "total": len(items),
        "items": items,
        "file_options": file_options,
    }


def delete_json_files(service, paths: list[str]) -> dict[str, Any]:
    if not paths:
        raise ValueError("请先选择要删除的 JSON 文件")

    allow = {os.path.abspath(p) for p in glob.glob("accounts_*.json")}
    selected = [os.path.abspath(str(p)) for p in paths]

    removed_files = 0
    removed_lines = 0
    skipped: list[str] = []
    all_emails: set[str] = set()
    removed_names: set[str] = set()

    for fp in selected:
        if fp not in allow:
            skipped.append(fp)
            continue
        if not os.path.isfile(fp):
            skipped.append(fp)
            continue
        all_emails |= emails_from_accounts_json(fp)
        try:
            os.remove(fp)
            removed_files += 1
            removed_names.add(os.path.basename(fp))
        except Exception:
            skipped.append(fp)

    if removed_names:
        with service._lock:
            notes = service._normalize_json_file_notes(service.cfg.get("json_file_notes") or {})
            changed = False
            for name in removed_names:
                if name in notes:
                    notes.pop(name, None)
                    changed = True
            if changed:
                service.cfg["json_file_notes"] = notes
                save_config(service.cfg)

    acct_path = accounts_txt_path(service)
    if all_emails and os.path.isfile(acct_path):
        email_lower = {e.lower() for e in all_emails}
        try:
            with open(acct_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            kept: list[str] = []
            for raw in lines:
                line = raw.strip()
                if not line:
                    continue
                ep = line.split("----", 1)[0].strip().lower()
                if ep in email_lower:
                    removed_lines += 1
                    continue
                kept.append(raw if raw.endswith("\n") else raw + "\n")
            with open(acct_path, "w", encoding="utf-8") as f:
                f.writelines(kept)
        except Exception as e:
            service.log(f"更新 {acct_path} 失败: {e}")

    service.log(
        f"已删除 {removed_files} 个 JSON；从账号列表移除 {removed_lines} 行（{acct_path}）"
    )
    return {
        "removed_files": removed_files,
        "removed_lines": removed_lines,
        "skipped": skipped,
    }


def sync_selected_accounts(
    service,
    emails: list[str],
    provider_override: str = "",
) -> dict[str, Any]:
    selected = [str(e).strip().lower() for e in emails if str(e).strip()]
    if not selected:
        raise ValueError("请先勾选要同步的账号")

    with service._lock:
        if service._sync_busy:
            raise RuntimeError("同步正在进行中，请稍候")
        service._sync_busy = True

    ok = 0
    fail = 0
    missing: list[str] = []
    try:
        remote_provider = _normalize_remote_account_provider(
            provider_override or (service.cfg or {}).get("remote_account_provider") or "sub2api"
        )
        emails_uniq = list(dict.fromkeys(selected))
        local_map = build_local_account_index(service)

        found_accounts: list[dict[str, Any]] = []
        for em in emails_uniq:
            acc = local_map.get(em)
            if not acc:
                missing.append(em)
                continue
            found_accounts.append(acc)

        for em in missing:
            service.log(f"同步跳过 {em}: 本地 JSON 中未找到该账号详情")

        if not found_accounts:
            fail = len(emails_uniq)
            raise RuntimeError("本地 JSON 中未找到可同步账号")

        if remote_provider == "cliproxyapi":
            base, auth, verify_ssl, proxy_arg = service._cliproxy_management_context()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            }

            for idx, acc in enumerate(found_accounts, start=1):
                row = _account_to_codex_record(acc)
                email = str(row.get("email") or "").strip().lower()
                if not email:
                    fail += 1
                    continue
                stem = _safe_export_stem(email, f"codex_account_{idx}")
                file_name = f"{stem}.json"
                body = json.dumps(row, ensure_ascii=False).encode("utf-8")
                q = urllib.parse.urlencode({"name": file_name})
                url = f"{base.rstrip('/')}/auth-files?{q}"
                code, text = _http_post_json(
                    url,
                    body,
                    headers,
                    verify_ssl=verify_ssl,
                    proxy=proxy_arg,
                )
                success = 200 <= code < 300
                if success and (text or "").strip():
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict) and payload.get("error"):
                        success = False
                if success:
                    ok += 1
                else:
                    fail += 1
                    snippet = (text or "")[:220].replace("\n", " ")
                    service.log(f"CLIProxyAPI 导入失败 {email}: HTTP {code} {snippet}")

            fail += len(missing)
            service.log(f"CLIProxyAPI 导入完成：成功 {ok}，失败 {fail}")
            return {"ok": ok, "fail": fail, "missing": missing}

        url = str(service.cfg.get("accounts_sync_api_url") or "").strip()
        tok = str(service.cfg.get("accounts_sync_bearer_token") or "").strip()
        verify_ssl = bool(service.cfg.get("openai_ssl_verify", True))
        proxy_arg = str(service.cfg.get("proxy") or "").strip() or None

        if not url:
            raise ValueError("请先填写同步 API 地址")
        if not tok:
            raise ValueError("请先填写 Bearer Token")

        auth = tok if tok.lower().startswith("bearer ") else f"Bearer {tok}"

        payload = {
            "data": {"accounts": found_accounts, "proxies": []},
            "skip_default_group_bind": True,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": auth,
        }
        code, text = _http_post_json(
            url,
            body,
            headers,
            verify_ssl=verify_ssl,
            proxy=proxy_arg,
        )
        if 200 <= code < 300:
            ok = len(found_accounts)
            fail = len(missing)
            service.log(f"批量同步成功 HTTP {code}，账号 {ok} 个")
        else:
            fail = len(found_accounts) + len(missing)
            snippet = (text or "")[:500].replace("\n", " ")
            raise RuntimeError(f"批量同步失败 HTTP {code} {snippet}")

        return {"ok": ok, "fail": fail, "missing": missing}
    finally:
        with service._lock:
            service._sync_busy = False
        service.log(f"同步结束：成功 {ok}，失败 {fail}")


__all__ = [
    "accounts_txt_path",
    "build_email_source_files_map",
    "build_local_account_index",
    "delete_json_files",
    "email_from_account_entry",
    "emails_from_accounts_json",
    "list_accounts",
    "list_json_files",
    "export_codex_accounts",
    "save_json_file_note",
    "source_label",
    "sync_selected_accounts",
]
