#!/usr/bin/env python3
"""
Fix subscriptions that are verified but marked as inactive.
This script sets is_active=True for all verified subscriptions.
"""
import sys
import os

# Add parent directory to path so we can import webapp
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
sys.path.insert(0, parent_dir)

from webapp.models import SessionLocal, Subscription

def fix_inactive_verified_subscriptions():
    """Set is_active=True for all verified subscriptions."""
    db = SessionLocal()
    try:
        # Find all subscriptions that are verified but inactive
        subscriptions = db.query(Subscription).filter(
            Subscription.is_verified == True,
            Subscription.is_active == False
        ).all()

        if not subscriptions:
            print("No verified but inactive subscriptions found.")
            return

        print(f"Found {len(subscriptions)} verified but inactive subscriptions:")
        print("=" * 80)

        for sub in subscriptions:
            print(f"ID: {sub.id} | Email: {sub.email} | Region: {sub.place_display_name or sub.region}")

        print("=" * 80)
        confirm = input(f"Set is_active=True for these {len(subscriptions)} subscriptions? (yes/no): ")

        if confirm.lower() != 'yes':
            print("Aborted.")
            return

        # Update all subscriptions
        count = 0
        for sub in subscriptions:
            sub.is_active = True
            count += 1

        db.commit()
        print(f"\nSuccessfully updated {count} subscriptions.")
        print("These subscriptions will now receive reports as scheduled.")

    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    fix_inactive_verified_subscriptions()
