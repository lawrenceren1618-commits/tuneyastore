#!/usr/bin/env python3
"""Download UPS Proof-of-Delivery PDFs via local Chromium/Playwright (no Bright Data).

Usage:
  python scripts/print_ups_pod_local.py --workers 2 1ZK3526F6818647950
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from playwright.sync_api import Browser, Page, sync_playwright

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore


TN_RE = re.compile(r"1Z[A-Z0-9]{16}", re.IGNORECASE)
DECLARED_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*件货件")
DECLARED_EN_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*package", re.IGNORECASE)

STATUS_DELAYED = "延迟"
STATUS_IN_TRANSIT = "在途"
STATUS_DOWNLOADED = "已下载PDF"
STATUS_FAILED = "失败"

POD_MARKERS_ZH = ("服务", "递送于", "递送至", "收件人")
POD_MARKERS_EN = ("Service", "Delivered On", "Delivered To", "Received By")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Playwright's bundled chromium-headless-shell hits ERR_HTTP2_PROTOCOL_ERROR
# against ups.com Akamai from some cloud egress; system Chrome works.
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]


@dataclass
class ResultRow:
    tn: str
    result: str
    note: str = ""
    pdf: str = ""
    validated: str = ""


@dataclass
class RunState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    rows: List[ResultRow] = field(default_factory=list)


def track_url(tn: str, loc: str = "zh_CN") -> str:
    return f"https://www.ups.com/track?loc={loc}&trackNums={tn}"


def launch_browser(p) -> Browser:
    try:
        return p.chromium.launch(headless=True, channel="chrome", args=LAUNCH_ARGS)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] channel=chrome failed ({exc}); trying bundled chromium", flush=True)
        return p.chromium.launch(headless=True, args=LAUNCH_ARGS + ["--disable-http2"])


def new_page(browser: Browser) -> Page:
    context = browser.new_context(
        locale="zh-CN",
        user_agent=UA,
        viewport={"width": 1400, "height": 900},
        accept_downloads=True,
    )
    page = context.new_page()
    page.set_default_timeout(60000)
    return page


def with_own_browser(fn):
    """Run fn(browser) inside a thread-local Playwright + Chrome instance."""
    with sync_playwright() as p:
        browser = launch_browser(p)
        try:
            return fn(browser)
        finally:
            browser.close()


def dismiss_noise(page: Page) -> None:
    for sel in (
        "#onetrust-accept-btn-handler",
        "button:has-text('接受所有 Cookie')",
        "button:has-text('Accept All')",
        "button:has-text('全部允许')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
    try:
        loc = page.locator("button:has-text('不')").first
        if loc.count() and loc.is_visible():
            loc.click(timeout=1000)
            page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass


def goto_track(page: Page, tn: str, loc: str = "zh_CN") -> None:
    page.goto(track_url(tn, loc), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2500)
    dismiss_noise(page)
    try:
        page.wait_for_selector(f"text={tn}", timeout=30000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(3000)


def parse_declared_total(text: str) -> Optional[int]:
    m = DECLARED_RE.search(text) or DECLARED_EN_RE.search(text)
    if not m:
        return None
    return int(m.group(2))


def extract_visible_tns(page: Page) -> List[str]:
    found = page.evaluate(
        r"""() => {
          const out = [];
          for (const el of document.querySelectorAll('a, span, div, button, p, td, li')) {
            const t = (el.textContent || '').trim();
            const m = t.match(/^1Z[A-Z0-9]{16}$/i);
            if (m) out.push(m[0].toUpperCase());
          }
          return out;
        }"""
    )
    seen = set()
    ordered: List[str] = []
    for tn in found:
        u = tn.upper()
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def collect_shipment_tns(page: Page, seed: str) -> Tuple[List[str], Optional[int]]:
    seed = seed.upper()
    goto_track(page, seed)
    body = page.inner_text("body")
    declared = parse_declared_total(body)

    expanded = False
    for label in (
        "All Packages in this Shipment",
        "此货件中的所有包裹",
        "此货件中的所有货件",
    ):
        loc = page.get_by_text(label, exact=False).first
        try:
            if loc.count():
                loc.click(timeout=5000)
                page.wait_for_timeout(1500)
                expanded = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not expanded:
        page.evaluate(
            """() => {
              const el = [...document.querySelectorAll('*')].find(
                e => (e.innerText || '').includes('All Packages in this Shipment')
              );
              if (el) el.click();
            }"""
        )
        page.wait_for_timeout(1500)

    all_tns: List[str] = []
    for _ in range(30):
        for tn in extract_visible_tns(page):
            if tn not in all_tns:
                all_tns.append(tn)

        if declared and len(all_tns) >= declared:
            break

        clicked = page.evaluate(
            """() => {
              const nexts = [...document.querySelectorAll('button, a, [role=button]')].filter(e => {
                const aria = (e.getAttribute('aria-label') || '').toLowerCase();
                const cls = (e.className || '').toString();
                const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
                return aria === 'next'
                  || cls.includes('ups-pagination-btn_next')
                  || /^下一步|Next$/i.test(t);
              });
              for (const n of nexts) {
                const visible = !!(n.offsetParent || n.getClientRects().length);
                if (!visible || n.disabled || n.getAttribute('aria-disabled') === 'true') {
                  continue;
                }
                n.click();
                return true;
              }
              return false;
            }"""
        )
        if not clicked:
            break
        page.wait_for_timeout(2000)

    if seed not in all_tns:
        all_tns.insert(0, seed)

    body2 = page.inner_text("body")
    declared2 = parse_declared_total(body2) or declared
    return all_tns, declared2


def classify_status(page: Page) -> Tuple[str, str]:
    text = page.inner_text("body")
    delivered = bool(re.search(r"已递送|已派送|Delivered", text))
    has_pod = bool(
        page.locator("text=递送证明").count()
        or page.locator("text=Proof of Delivery").count()
    )

    if delivered or has_pod:
        return "delivered", "已递送" + ("+递送证明" if has_pod else "")

    if re.search(r"延迟|Delayed", text):
        return "delayed", "页面含延迟状态"
    if re.search(r"在途|In Transit|On the Way|On Its Way|运输中|正在运送", text, re.I):
        return "in_transit", "页面含在途状态"
    return "unknown", "无法判定状态"


def open_pod_modal(page: Page) -> bool:
    for label in ("递送证明", "Proof of Delivery"):
        loc = page.get_by_text(label, exact=False).first
        try:
            if loc.count() and loc.is_visible():
                loc.click(timeout=8000)
                page.wait_for_selector("#stApp_podModal", state="visible", timeout=20000)
                page.wait_for_timeout(800)
                return True
        except Exception:  # noqa: BLE001
            continue
    clicked = page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('a, button, span, div')].find(e => {
            const t = (e.innerText || '').trim();
            return t === '递送证明' || t.startsWith('递送证明') || /Proof of Delivery/i.test(t);
          });
          if (!el) return false;
          el.click();
          return true;
        }"""
    )
    if not clicked:
        return False
    try:
        page.wait_for_selector("#stApp_podModal", state="visible", timeout=20000)
        page.wait_for_timeout(800)
        return True
    except Exception:  # noqa: BLE001
        return False


def prepare_pod_print_dom(page: Page) -> None:
    """Clone #stApp_podModal content and hide other nodes for page.pdf."""
    page.evaluate(
        """() => {
          const modal = document.querySelector('#stApp_podModal');
          if (!modal) throw new Error('#stApp_podModal not found');
          const clone = modal.cloneNode(true);
          clone.id = 'stApp_podModal_printClone';
          clone.querySelectorAll('button, a').forEach(el => {
            const t = (el.innerText || '');
            if (/关闭|打印|Close|Print|chevron/i.test(t)) el.remove();
          });
          [...document.body.children].forEach(ch => {
            ch.style.setProperty('display', 'none', 'important');
          });
          document.querySelectorAll(
            '#onetrust-banner-sdk, .ot-sdk-container, .WACMainWindowModalHost, #sa-flyout-panel'
          ).forEach(el => el.style.setProperty('display', 'none', 'important'));
          document.body.appendChild(clone);
          document.body.style.background = '#ffffff';
          document.body.style.color = '#000000';
          document.body.style.padding = '24px';
          document.documentElement.style.background = '#ffffff';
        }"""
    )


def print_pod_pdf(page: Page, out_path: Path) -> None:
    prepare_pod_print_dom(page)
    page.pdf(
        path=str(out_path),
        format="A4",
        print_background=False,
        display_header_footer=True,
        header_template=(
            '<div style="font-size:8px;width:100%;text-align:center;color:#666;">'
            '<span class="title"></span></div>'
        ),
        footer_template=(
            '<div style="font-size:8px;width:100%;text-align:center;color:#666;">'
            '<span class="url"></span> | '
            '<span class="pageNumber"></span>/<span class="totalPages"></span></div>'
        ),
        margin={"top": "48px", "bottom": "48px", "left": "24px", "right": "24px"},
    )


def pdf_body_text(path: Path) -> str:
    doc = pymupdf.open(path)
    parts: List[str] = []
    for i in range(doc.page_count):
        parts.append(doc.load_page(i).get_text("text"))
    doc.close()
    raw = "\n".join(parts)
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # Footer URLs / chrome do not count toward validation markers.
        if re.search(r"https?://|www\.ups\.com|Tracking \| UPS", s, re.I):
            continue
        if re.fullmatch(r"\d+/\d+", s):
            continue
        lines.append(s)
    return "\n".join(lines)


def validate_pod_pdf(path: Path) -> Tuple[bool, str]:
    text = pdf_body_text(path)
    if not text.strip():
        return False, "PDF无有效正文"
    zh_ok = all(m in text for m in POD_MARKERS_ZH)
    en_ok = all(m in text for m in POD_MARKERS_EN)
    if zh_ok or en_ok:
        return True, "校验通过"
    missing_zh = [m for m in POD_MARKERS_ZH if m not in text]
    missing_en = [m for m in POD_MARKERS_EN if m not in text]
    return False, f"缺少字段 zh={missing_zh} en={missing_en}"


def write_csv(path: Path, rows: Sequence[ResultRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["追踪编号", "结果", "校验", "PDF", "备注"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "追踪编号": r.tn,
                    "结果": r.result,
                    "校验": r.validated,
                    "PDF": r.pdf,
                    "备注": r.note,
                }
            )


def process_tn(tn: str, out_dir: Path) -> ResultRow:
    """Two-phase per TN: probe status, then print PoD PDF if delivered."""

    def _run(browser: Browser) -> ResultRow:
        page = new_page(browser)
        pdf_path = out_dir / f"{tn}.pdf"
        try:
            print(f"[probe] {tn}", flush=True)
            goto_track(page, tn)
            bucket, detail = classify_status(page)
            print(f"[probe] {tn} -> {bucket} ({detail})", flush=True)

            if bucket == "delayed":
                return ResultRow(tn=tn, result=STATUS_DELAYED, note=detail)
            if bucket == "in_transit":
                return ResultRow(tn=tn, result=STATUS_IN_TRANSIT, note=detail)
            if bucket != "delivered":
                return ResultRow(tn=tn, result=STATUS_FAILED, note=f"非已递送: {detail}")

            print(f"[download] {tn}", flush=True)
            if not open_pod_modal(page):
                return ResultRow(tn=tn, result=STATUS_FAILED, note="未找到/无法打开递送证明")

            print_pod_pdf(page, pdf_path)
            ok, msg = validate_pod_pdf(pdf_path)
            if not ok:
                return ResultRow(
                    tn=tn,
                    result=STATUS_FAILED,
                    note=f"PDF已写出但校验失败: {msg}",
                    pdf=pdf_path.name,
                    validated="否",
                )
            print(f"[download] {tn} -> OK", flush=True)
            return ResultRow(
                tn=tn,
                result=STATUS_DOWNLOADED,
                note="OK",
                pdf=pdf_path.name,
                validated="是",
            )
        except Exception as exc:  # noqa: BLE001
            return ResultRow(
                tn=tn,
                result=STATUS_FAILED,
                note=f"{type(exc).__name__}: {exc}",
            )
        finally:
            page.context.close()

    return with_own_browser(_run)


def process_all(seed: str, out_dir: Path, workers: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state = RunState()

    def _collect(browser: Browser) -> Tuple[List[str], Optional[int]]:
        page = new_page(browser)
        try:
            print(f"[collect] opening seed {seed}", flush=True)
            return collect_shipment_tns(page, seed)
        finally:
            page.context.close()

    tns, declared = with_own_browser(_collect)
    print(f"[collect] declared={declared} parsed={len(tns)} tns={tns}", flush=True)
    if declared is not None and len(tns) != declared:
        print(
            f"[warn] TN count mismatch: declared {declared} vs parsed {len(tns)}",
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(process_tn, tn, out_dir) for tn in tns]
        for fut in as_completed(futs):
            row = fut.result()
            with state.lock:
                state.rows.append(row)
            print(
                f"[done] {row.tn} | {row.result} | 校验={row.validated or '-'} | {row.note}",
                flush=True,
            )

    order = {tn: i for i, tn in enumerate(tns)}
    state.rows.sort(key=lambda r: order.get(r.tn, 10_000))

    csv_path = out_dir / "结果清单.csv"
    write_csv(csv_path, state.rows)

    return {
        "declared": declared,
        "parsed": len(tns),
        "tns": tns,
        "downloaded": sum(1 for r in state.rows if r.result == STATUS_DOWNLOADED),
        "delayed": sum(1 for r in state.rows if r.result == STATUS_DELAYED),
        "in_transit": sum(1 for r in state.rows if r.result == STATUS_IN_TRANSIT),
        "failed": sum(1 for r in state.rows if r.result == STATUS_FAILED),
        "validated_ok": sum(1 for r in state.rows if r.validated == "是"),
        "csv": str(csv_path),
        "out_dir": str(out_dir),
        "rows": state.rows,
    }


def check_reachability() -> Tuple[bool, str]:
    try:
        with sync_playwright() as p:
            browser = launch_browser(p)
            page = new_page(browser)
            try:
                resp = page.goto(
                    "https://www.ups.com/track?loc=zh_CN&trackNums=1Z",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                status = resp.status if resp else None
                title = page.title()
                ok = status is not None and status < 500
                return ok, f"status={status} title={title!r}"
            finally:
                page.context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="UPS PoD PDF downloader (local Chromium)")
    parser.add_argument("seed", help="Seed UPS tracking number (1Z...)")
    parser.add_argument("--workers", type=int, default=2, help="Parallel workers (default 2)")
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("output/pods"),
        help="Output root directory",
    )
    parser.add_argument(
        "--skip-reachability",
        action="store_true",
        help="Skip ups.com reachability probe",
    )
    args = parser.parse_args(argv)

    seed = args.seed.strip().upper()
    if not TN_RE.fullmatch(seed):
        print(f"Invalid tracking number: {seed}", file=sys.stderr)
        return 2

    workers = max(1, min(args.workers, 3))
    day = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    out_dir = args.out_root / f"{seed}+{day}"

    print(f"seed={seed} workers={workers} out={out_dir}", flush=True)

    if not args.skip_reachability:
        ok, detail = check_reachability()
        print(f"[reachability] ups.com reachable={ok} ({detail})", flush=True)
        if not ok:
            print("ERROR: ups.com not reachable from this VM", file=sys.stderr)
            return 3

    try:
        summary = process_all(seed, out_dir, workers)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    print("\n=== SUMMARY ===", flush=True)
    print(f"declared={summary['declared']} parsed={summary['parsed']}", flush=True)
    print(
        f"downloaded={summary['downloaded']} delayed={summary['delayed']} "
        f"in_transit={summary['in_transit']} failed={summary['failed']} "
        f"validated_ok={summary['validated_ok']}",
        flush=True,
    )
    print(f"out_dir={summary['out_dir']}", flush=True)
    print(f"csv={summary['csv']}", flush=True)
    for r in summary["rows"]:
        print(f"  {r.tn} | {r.result} | 校验={r.validated or '-'} | {r.note}", flush=True)

    if (
        summary["failed"]
        and summary["downloaded"] == 0
        and summary["delayed"] + summary["in_transit"] == 0
    ):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
