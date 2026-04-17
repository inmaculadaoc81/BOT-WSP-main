"""
Quick integration tests to verify services can connect and work.
Run with: python tests.py
"""
import asyncio
import sys
import os

# Ensure we load .env
from dotenv import load_dotenv
load_dotenv()

from config import settings
from sheets_service import SheetsService
from odoo_service import OdooService


async def test_config():
    """Test that all required config values are set."""
    print("=" * 50)
    print("TEST: Configuration")
    print("=" * 50)

    errors = []

    if not settings.GOOGLE_SHEETS_ID:
        errors.append("GOOGLE_SHEETS_ID is empty")
    else:
        print(f"  GOOGLE_SHEETS_ID: {settings.GOOGLE_SHEETS_ID[:10]}...")

    if not settings.GOOGLE_PRICES_SHEET_ID:
        errors.append("GOOGLE_PRICES_SHEET_ID is empty")
    else:
        print(f"  GOOGLE_PRICES_SHEET_ID: {settings.GOOGLE_PRICES_SHEET_ID[:10]}...")

    if not settings.ODOO_URL:
        errors.append("ODOO_URL is empty")
    else:
        print(f"  ODOO_URL: {settings.ODOO_URL}")

    if not settings.CHATWOOT_URL:
        errors.append("CHATWOOT_URL is empty")
    else:
        print(f"  CHATWOOT_URL: {settings.CHATWOOT_URL}")

    if not settings.OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is empty")
    else:
        print(f"  OPENAI_API_KEY: {settings.OPENAI_API_KEY[:8]}...")

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        return False

    print("  PASS")
    return True


async def test_repairs_sheet():
    """Test connection to repairs Google Sheet."""
    print("\n" + "=" * 50)
    print("TEST: Repairs Sheet (Google Sheets)")
    print("=" * 50)

    try:
        svc = SheetsService()
        await svc.connect()

        if not svc._client:
            print("  FAIL: Could not connect to Google Sheets")
            return False

        records = await svc._fetch_all_records()
        print(f"  Fetched {len(records)} repair records")

        if records:
            cols = list(records[0].keys())
            print(f"  Columns: {cols[:5]}...")
            print("  PASS")
            return True
        else:
            print("  WARNING: Sheet is empty (0 records)")
            return True

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def test_prices_sheet():
    """Test connection to prices Google Sheet."""
    print("\n" + "=" * 50)
    print("TEST: Prices Sheet (Google Sheets)")
    print("=" * 50)

    try:
        svc = SheetsService()
        await svc.connect()

        if not svc._client:
            print("  FAIL: Could not connect to Google Sheets")
            return False

        prices = await svc.get_all_prices()
        print(f"  Fetched {len(prices)} price records")

        if prices:
            sample = prices[0]
            print(f"  Sample: {sample['marca']} {sample['modelo']} - {sample['tipo_reparacion']} - {sample['precio']}")

            # Verify format_prices_for_prompt works
            prompt = svc.format_prices_for_prompt(prices)
            lines = prompt.split("\n")
            print(f"  Prompt generated: {len(lines)} lines")
            print("  PASS")
            return True
        else:
            print("  FAIL: No price records found")
            return False

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def test_odoo_auth():
    """Test Odoo authentication."""
    print("\n" + "=" * 50)
    print("TEST: Odoo Authentication")
    print("=" * 50)

    try:
        svc = OdooService()
        session = await svc._authenticate()

        if session:
            print(f"  Session cookie: {session[:30]}...")
            print("  PASS")
            return True
        else:
            print("  FAIL: No session cookie returned")
            return False

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def main():
    print("\nRunning integration tests...\n")

    results = []
    results.append(("Config", await test_config()))
    results.append(("Repairs Sheet", await test_repairs_sheet()))
    results.append(("Prices Sheet", await test_prices_sheet()))
    results.append(("Odoo Auth", await test_odoo_auth()))

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)

    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed!")
    else:
        print("Some tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
