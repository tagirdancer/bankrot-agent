"""Полная диагностика лота: login + все PDF + парсинг."""
import asyncio, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(HERE), "bankrot_agent", ".env")
if os.path.isfile(ENV):
    for line in open(ENV, encoding="utf-8"):
        if line.strip() and "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)
sys.path.insert(0, HERE)

LOT = sys.argv[1] if len(sys.argv) > 1 else "7618864"


async def main():
    from playwright.async_api import async_playwright
    from agent import enrich, login, launch_browser, _try_lot_pdfs
    from analyzer import analyze_lot, resolve_document_status, format_egrn_legal_block
    from egrn_pdf import discover_pdf_urls

    lot = {
        "id": LOT, "url": f"https://tbankrot.ru/item?id={LOT}",
        "title": "", "description": "", "region": "moskva",
    }
    report = {"lot_id": LOT, "url": lot["url"]}

    async with async_playwright() as p:
        b = await launch_browser(p)
        ctx = await b.new_context(user_agent="Mozilla/5.0 Chrome/120")
        page = await ctx.new_page()
        report["login_ok"] = await login(page)
        await page.goto(lot["url"], timeout=35000)
        await page.wait_for_timeout(2500)
        html = await page.content()
        report["pdf_urls_on_page"] = discover_pdf_urls(html, LOT)
        report["page_has_login_wall"] = (
            "войдите или зарегистрируйтесь" in html.lower()
            or "для просмотра полной информации" in html.lower()
        )
        await enrich(lot, page, ctx, heavy=True)
        await b.close()

    an = await analyze_lot(lot, light=False)
    report["pdfs_downloaded_count"] = lot.get("pdfs_downloaded_count", 0)
    report["pdfs_downloaded"] = lot.get("pdfs_downloaded", [])
    report["egrn_parsed"] = lot.get("egrn_parsed", {})
    report["appraisal_parsed"] = lot.get("appraisal_parsed", {})
    report["document_status"] = resolve_document_status(lot)
    report["legal_block"] = format_egrn_legal_block(lot.get("egrn_parsed") or {})
    report["analysis"] = {
        "score": an.get("total_score"),
        "price_line": an.get("price_line"),
        "market_known": an.get("market_known"),
        "market_source": an.get("market_source"),
        "market_comment": an.get("market_comment"),
        "discount_pct": an.get("discount_pct"),
        "document_status": an.get("document_status"),
        "legal_text": an.get("legal_text"),
    }
    report["lot_summary"] = {
        "title": lot.get("title"),
        "price": lot.get("price"),
        "cadastral": lot.get("cadastral"),
        "address": lot.get("address"),
    }

    out = os.path.join(HERE, f"test_lot_{LOT}.json")
    open(out, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
