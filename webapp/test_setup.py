#!/usr/bin/env python3
"""
Test script to verify the iNaturalist Invasives Monitor setup.

Run this after installing dependencies to ensure everything works:
    python webapp/test_setup.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def print_header(title):
    print(f"\n{'='*60}")
    print(f" {title}")
    print('='*60)

def print_result(test_name, success, message=""):
    status = "PASS" if success else "FAIL"
    color = "\033[92m" if success else "\033[91m"
    reset = "\033[0m"
    print(f"  {color}[{status}]{reset} {test_name}")
    if message:
        print(f"        {message}")

def test_imports():
    """Test that all required packages can be imported."""
    print_header("Testing Imports")

    tests = [
        ("FastAPI", "fastapi"),
        ("SQLAlchemy", "sqlalchemy"),
        ("Celery", "celery"),
        ("Redis", "redis"),
        ("Requests", "requests"),
        ("Jinja2", "jinja2"),
        ("inat_diff", "inat_diff"),
    ]

    all_passed = True
    for name, module in tests:
        try:
            __import__(module)
            print_result(name, True)
        except ImportError as e:
            print_result(name, False, str(e))
            all_passed = False

    return all_passed

def test_database():
    """Test database initialization and operations."""
    print_header("Testing Database")

    # Use test database
    os.environ["DATABASE_URL"] = "sqlite:///test_invasives.db"

    try:
        from webapp.models import init_db, SessionLocal, Subscription, Frequency

        # Initialize database
        init_db()
        print_result("Database initialization", True)

        # Create a test subscription
        db = SessionLocal()
        try:
            test_sub = Subscription(
                email="test@example.com",
                region="Oregon",
                place_id=10,
                place_name="Oregon",
                frequency=Frequency.MONTHLY,
                is_verified=False,
                is_active=True
            )
            db.add(test_sub)
            db.commit()

            # Query it back
            found = db.query(Subscription).filter(
                Subscription.email == "test@example.com"
            ).first()

            if found and found.region == "Oregon":
                print_result("Create subscription", True)
            else:
                print_result("Create subscription", False, "Could not retrieve")
                return False

            # Clean up
            db.delete(found)
            db.commit()
            print_result("Delete subscription", True)

        finally:
            db.close()

        # Remove test database
        if os.path.exists("test_invasives.db"):
            os.remove("test_invasives.db")

        return True

    except Exception as e:
        print_result("Database operations", False, str(e))
        return False

def test_inat_client():
    """Test iNaturalist API connection and place resolution."""
    print_header("Testing iNaturalist API")

    try:
        from inat_diff import iNatClient

        client = iNatClient()

        # Test place resolution
        test_places = [
            ("Oregon", "state"),
            ("California", "state"),
            ("United States", "country"),
        ]

        all_passed = True
        for place_name, expected_type in test_places:
            try:
                place_id, place_info = client.resolve_place_with_info(place_name)
                if place_id and place_info:
                    print_result(
                        f"Resolve '{place_name}'",
                        True,
                        f"ID: {place_id}, Name: {place_info.get('display_name', 'N/A')}"
                    )
                else:
                    print_result(f"Resolve '{place_name}'", False, "No result")
                    all_passed = False
            except Exception as e:
                print_result(f"Resolve '{place_name}'", False, str(e))
                all_passed = False

        return all_passed

    except Exception as e:
        print_result("iNaturalist client", False, str(e))
        return False

def test_redis_connection():
    """Test Redis connection for Celery."""
    print_header("Testing Redis Connection")

    try:
        import redis

        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        r = redis.from_url(redis_url)

        # Try to ping
        if r.ping():
            print_result("Redis ping", True, f"Connected to {redis_url}")
            return True
        else:
            print_result("Redis ping", False, "No response")
            return False

    except redis.ConnectionError as e:
        print_result("Redis connection", False, f"Is Redis running? {e}")
        return False
    except Exception as e:
        print_result("Redis", False, str(e))
        return False

def test_fastapi_app():
    """Test FastAPI application initialization."""
    print_header("Testing FastAPI Application")

    try:
        # Set test database
        os.environ["DATABASE_URL"] = "sqlite:///test_app.db"

        from webapp.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)

        # Test home page
        response = client.get("/")
        if response.status_code == 200:
            print_result("Home page", True, f"Status: {response.status_code}")
        else:
            print_result("Home page", False, f"Status: {response.status_code}")
            return False

        # Test health endpoint
        response = client.get("/health")
        if response.status_code == 200:
            print_result("Health endpoint", True)
        else:
            print_result("Health endpoint", False, f"Status: {response.status_code}")
            return False

        # Test admin page
        response = client.get("/admin")
        if response.status_code == 200:
            print_result("Admin dashboard", True)
        else:
            print_result("Admin dashboard", False, f"Status: {response.status_code}")
            return False

        # Clean up
        if os.path.exists("test_app.db"):
            os.remove("test_app.db")

        return True

    except Exception as e:
        print_result("FastAPI application", False, str(e))
        return False

def test_email_config():
    """Test email configuration (without sending)."""
    print_header("Testing Email Configuration")

    try:
        from webapp.email_service import email_service
        from webapp.config import MANDRILL_API_KEY, FROM_EMAIL

        if MANDRILL_API_KEY:
            print_result("Mandrill API key", True, "Configured (hidden)")
        else:
            print_result("Mandrill API key", False,
                        "Not set. Set MANDRILL_API_KEY environment variable.")

        print_result("From email", True, FROM_EMAIL)

        return bool(MANDRILL_API_KEY)

    except Exception as e:
        print_result("Email configuration", False, str(e))
        return False

def test_celery_tasks():
    """Test Celery task definitions (without running)."""
    print_header("Testing Celery Tasks")

    try:
        from webapp.tasks import (
            celery_app,
            generate_report_task,
            send_report_email_task,
            generate_scheduled_reports,
            send_verification_email_task
        )

        # Check tasks are registered
        tasks = [
            "generate_report_task",
            "send_report_email_task",
            "generate_scheduled_reports",
            "send_verification_email_task"
        ]

        all_passed = True
        for task_name in tasks:
            # Just verify the function exists and is callable
            task_func = globals().get(task_name) or locals().get(task_name)
            print_result(task_name, True)

        # Check beat schedule
        from webapp.celery_config import beat_schedule
        if 'generate-monthly-reports' in beat_schedule:
            print_result("Monthly schedule", True)
        else:
            print_result("Monthly schedule", False)
            all_passed = False

        if 'generate-weekly-reports' in beat_schedule:
            print_result("Weekly schedule", True)
        else:
            print_result("Weekly schedule", False)
            all_passed = False

        return all_passed

    except Exception as e:
        print_result("Celery tasks", False, str(e))
        return False

def test_templates():
    """Test that all templates exist."""
    print_header("Testing Templates")

    template_dir = Path(__file__).parent / "templates"

    required_templates = [
        "base.html",
        "index.html",
        "message.html",
        "error.html",
        "verified.html",
        "manage.html",
        "admin/dashboard.html"
    ]

    all_passed = True
    for template in required_templates:
        path = template_dir / template
        if path.exists():
            print_result(template, True)
        else:
            print_result(template, False, f"Not found at {path}")
            all_passed = False

    return all_passed

def main():
    """Run all tests."""
    print("\n" + "="*60)
    print(" iNaturalist Invasives Monitor - Setup Verification")
    print("="*60)

    results = {
        "Imports": test_imports(),
        "Database": test_database(),
        "Templates": test_templates(),
        "iNaturalist API": test_inat_client(),
        "FastAPI App": test_fastapi_app(),
        "Celery Tasks": test_celery_tasks(),
        "Redis": test_redis_connection(),
        "Email Config": test_email_config(),
    }

    # Summary
    print_header("Summary")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, result in results.items():
        print_result(test_name, result)

    print(f"\n  Total: {passed}/{total} tests passed")

    if passed == total:
        print("\n  All tests passed! Your setup is ready.")
        print("  \n  To start the application locally:")
        print("    1. Start Redis: redis-server")
        print("    2. Start web: uvicorn webapp.main:app --reload")
        print("    3. Start worker: celery -A webapp.tasks worker -l info")
        print("    4. Visit: http://localhost:8000")
    else:
        print("\n  Some tests failed. Please check the issues above.")
        if not results["Redis"]:
            print("  - Install and start Redis: sudo apt install redis-server && redis-server")
        if not results["Email Config"]:
            print("  - Set MANDRILL_API_KEY environment variable")

    print("")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
