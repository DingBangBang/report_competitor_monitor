"""Pipeline orchestration and CLI entry point for both data flows.

Two independent pipelines are exposed:

* ``report``      – collect KPIs and produce daily / weekly / monthly reports.
* ``competitor``  – scrape, analyse and report competitor announcements.

Each run pushes to Lark, emails the HTML, fires a macOS notification and records
a row in ``run_log``.  Failures never crash silently: the status is surfaced in
the notifications and the run log.
"""

from __future__ import annotations

import argparse
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.competitor_analyzer import CompetitorAnalyzer
from src.competitor_fetcher import CompetitorFetcher
from src.competitor_report_generator import CompetitorReportGenerator
from src.config import Config
from src.database import Database
from src.email_notifier import EmailNotifier
from src.lark_notifier import LarkNotifier
from src.mac_notifier import MacNotifier
from src.weekly_data_fetcher import WeeklyDataFetcher
from src.weekly_report_generator import REPORT_LABELS, WeeklyReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------
def due_report_types(now: datetime | None = None, force_all: bool = False) -> list[str]:
    """Return report types that should run today based on their name cadence.

    daily   -> every day
    weekly  -> Mondays
    monthly -> the 1st of the month
    """
    if force_all:
        return ["daily", "weekly", "monthly"]
    now = now or datetime.now(timezone.utc)
    types = ["daily"]
    if now.weekday() == 0:  # Monday
        types.append("weekly")
    if now.day == 1:
        types.append("monthly")
    return types


def report_url(filename: str) -> str:
    if Config.REPORT_BASE_URL:
        return Config.REPORT_BASE_URL.rstrip("/") + "/" + filename
    return str(Path(Config.REPORTS_DIR) / filename)


def _fmt_metric(m: dict[str, Any]) -> str:
    chg = m.get("change_pct")
    arrow = ""
    if chg is not None:
        arrow = f"（{'▲' if chg >= 0 else '▼'}{abs(chg):.1f}%）"
    return f"- **{m['metric_label']}**：{m['metric_value']}{m.get('metric_unit') or ''}{arrow}"


# ---------------------------------------------------------------------------
# Report pipeline
# ---------------------------------------------------------------------------
def run_report_pipeline(report_types: list[str], send_all_email: bool = False) -> dict[str, Any]:
    db = Database()
    fetcher = WeeklyDataFetcher(db)
    generator = WeeklyReportGenerator(db)
    lark = LarkNotifier()
    email = EmailNotifier()
    mac = MacNotifier()

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "SUCCESS"
    lark_ok_any = False
    email_status = "skipped"
    primary_headline = ""
    generated: list[dict[str, Any]] = []

    try:
        for rt in report_types:
            logger.info("=== Generating %s report ===", rt)
            fetcher.fetch_and_store(rt)
            ctx = generator.generate(rt)
            generated.append(ctx)
            primary_headline = primary_headline or ctx["headline"]

            link = report_url(ctx["filename"])
            body = (
                f"**核心结论**：{ctx['headline']}\n\n"
                + "\n".join(_fmt_metric(m) for m in ctx["metrics"][:6])
            )
            ok = lark.send_weekly_card(
                title=f"📊 数据{REPORT_LABELS.get(rt, rt)} · {ctx['period_end']}",
                content_md=body,
                buttons=[{"text": "查看完整报告", "url": link}],
            )
            lark_ok_any = lark_ok_any or ok

            if send_all_email or _email_due(rt):
                sent, detail = email.send_html(
                    subject=f"[数据{REPORT_LABELS.get(rt, rt)}] {ctx['period_start']}~{ctx['period_end']} | {ctx['headline'][:40]}",
                    html_body=Path(ctx["html_path"]).read_text(encoding="utf-8"),
                )
                email_status = detail
    except Exception as exc:  # noqa: BLE001
        status = "FAILED"
        primary_headline = primary_headline or "管道执行失败"
        logger.error("Report pipeline error: %s", exc)
        logger.error(traceback.format_exc())
        db.log_run("report", status, summary=str(exc), detail=traceback.format_exc(), started_at=started_at)

    _finalise_run(
        db=db, pipeline="report", status=status, conclusion=primary_headline,
        lark=lark, mac=mac, lark_used=lark_ok_any, email_status=email_status,
        started_at=started_at, generated=generated,
    )
    return {"status": status, "reports": [g["filename"] for g in generated], "headline": primary_headline}


def _email_due(report_type: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if report_type == "daily":
        return True
    if report_type == "weekly":
        return now.weekday() == 0
    if report_type == "monthly":
        return now.day == 1
    return True



# ---------------------------------------------------------------------------
# Competitor pipeline
# ---------------------------------------------------------------------------
def run_competitor_pipeline() -> dict[str, Any]:
    db = Database()
    fetcher = CompetitorFetcher(db)
    analyzer = CompetitorAnalyzer(db)
    generator = CompetitorReportGenerator(db)
    lark = LarkNotifier()
    email = EmailNotifier()
    mac = MacNotifier()

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "SUCCESS"
    lark_ok = False
    email_status = "skipped"
    conclusion = ""
    ctx: dict[str, Any] = {}

    try:
        fetch_result = fetcher.fetch_and_store()
        analyzer.analyze_pending()
        ctx = generator.generate(days=7)
        top3 = ctx["top_three"]
        conclusion = top3[0] if top3 else f"本周捕获 {ctx['total_events']} 条竞品动态"

        link = report_url(ctx["filename"])
        body = "**本周最需关注：**\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(top3[:3], 1))
        body += f"\n\n共监控到 **{ctx['total_events']}** 条竞品动态。"
        lark_ok = lark.send_competitor_card(
            title=f"🛰️ 竞品动态监控 · {ctx['period_end']}",
            content_md=body,
            buttons=[{"text": "查看完整报告", "url": link}],
        )

        # Competitor email is always sent (daily) and is a separate email.
        sent, detail = email.send_html(
            subject=f"[竞品监控] {ctx['period_start']}~{ctx['period_end']} | {conclusion[:40]}",
            html_body=Path(ctx["html_path"]).read_text(encoding="utf-8"),
        )
        email_status = detail
    except Exception as exc:  # noqa: BLE001
        status = "FAILED"
        conclusion = conclusion or "竞品监控管道执行失败"
        logger.error("Competitor pipeline error: %s", exc)
        logger.error(traceback.format_exc())
        db.log_run("competitor", status, summary=str(exc), detail=traceback.format_exc(), started_at=started_at)

    _finalise_run(
        db=db, pipeline="competitor", status=status, conclusion=conclusion,
        lark=lark, mac=mac, lark_used=lark_ok, email_status=email_status,
        started_at=started_at, generated=[ctx] if ctx else [],
    )
    return {"status": status, "report": ctx.get("filename", ""), "conclusion": conclusion}


# ---------------------------------------------------------------------------
# Shared finalisation (notifications + run log)
# ---------------------------------------------------------------------------
def _finalise_run(
    db: Database,
    pipeline: str,
    status: str,
    conclusion: str,
    lark: LarkNotifier,
    mac: MacNotifier,
    lark_used: bool,
    email_status: str,
    started_at: str,
    generated: list[dict[str, Any]],
) -> None:
    finished = datetime.now(timezone.utc)
    lark_status = "成功" if lark_used else ("失败" if Config.LARK_ENABLED else "未配置")
    email_status_txt = email_status

    # 1) Run-level Lark notification (success/failure) to the matching bot.
    run_line = (
        f"{'✅' if status == 'SUCCESS' else '❌'} 管道[{pipeline}]运行{status}\n"
        f"结论：{conclusion}\n"
        f"结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"邮件推送：{email_status_txt} | Lark推送：{lark_status}"
    )
    if pipeline == "competitor":
        lark.send_competitor_text(run_line)
    else:
        lark.send_weekly_text(run_line)

    # 2) macOS desktop notification with the 5 required fields.
    mac.notify_run_summary(
        conclusion=conclusion,
        status="成功" if status == "SUCCESS" else "失败",
        email_status=email_status_txt,
        lark_status=lark_status,
        finished_at=finished.replace(tzinfo=None),
    )

    summary = conclusion
    db.log_run(
        pipeline=pipeline,
        status=status,
        summary=summary,
        detail=f"reports={[g.get('filename') for g in generated]} email={email_status_txt} lark={lark_status}",
        started_at=started_at,
    )



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report & competitor monitoring pipelines")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Run the data report pipeline")
    p_report.add_argument(
        "--type",
        default="auto",
        choices=["auto", "all", "daily", "weekly", "monthly"],
        help="Which report(s) to generate (auto = frequency-based)",
    )
    p_report.add_argument("--bootstrap", action="store_true", help="Force all three reports + emails")

    sub.add_parser("competitor", help="Run the competitor monitoring pipeline")
    sub.add_parser("all", help="Run everything (bootstrap: all reports + competitor)")

    args = parser.parse_args(argv)

    if args.command == "report":
        if args.type == "all" or args.bootstrap:
            types, send_all = ["daily", "weekly", "monthly"], True
        elif args.type == "auto":
            types, send_all = due_report_types(), False
        else:
            types, send_all = [args.type], False
        result = run_report_pipeline(types, send_all_email=send_all)
        logger.info("Report pipeline finished: %s", result)
        return 0 if result["status"] == "SUCCESS" else 1

    if args.command == "competitor":
        result = run_competitor_pipeline()
        logger.info("Competitor pipeline finished: %s", result)
        return 0 if result["status"] == "SUCCESS" else 1

    if args.command == "all":
        r1 = run_report_pipeline(["daily", "weekly", "monthly"], send_all_email=True)
        r2 = run_competitor_pipeline()
        logger.info("Full run finished: report=%s competitor=%s", r1["status"], r2["status"])
        return 0 if r1["status"] == "SUCCESS" and r2["status"] == "SUCCESS" else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
