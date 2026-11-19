#!/usr/bin/env python3
"""
Management commands for iNaturalist Invasives Monitor.

Usage:
    python -m webapp.manage create-admin <username>      (prompts for password)
    python -m webapp.manage list-admins
    python -m webapp.manage delete-admin <username>
    python -m webapp.manage reset-password <username>    (prompts for password)
"""

import sys
import argparse
import os
import getpass
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp.models import init_db, SessionLocal, AdminUser
from webapp.auth import create_admin_user, hash_password


def cmd_create_admin(args):
    """Create a new admin user."""
    init_db()
    db = SessionLocal()

    try:
        admin = create_admin_user(db, args.username, args.password or _prompt_password())
        print(f"Admin user '{admin.username}' created successfully.")
        print(f"\nYou can now log in at /admin with these credentials.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        db.close()


def cmd_list_admins(args):
    """List all admin users."""
    init_db()
    db = SessionLocal()

    try:
        admins = db.query(AdminUser).all()

        if not admins:
            print("No admin users found.")
            print("\nCreate one with: python -m webapp.manage create-admin <username>")
            return

        print(f"Admin users ({len(admins)}):")
        print("-" * 40)
        for admin in admins:
            last_login = admin.last_login.strftime('%Y-%m-%d %H:%M') if admin.last_login else 'Never'
            print(f"  {admin.username}")
            print(f"    Created: {admin.created_at.strftime('%Y-%m-%d %H:%M')}")
            print(f"    Last login: {last_login}")
    finally:
        db.close()


def cmd_delete_admin(args):
    """Delete an admin user."""
    init_db()
    db = SessionLocal()

    try:
        admin = db.query(AdminUser).filter(AdminUser.username == args.username).first()

        if not admin:
            print(f"Error: Admin user '{args.username}' not found.")
            sys.exit(1)

        # Check if it's the last admin
        count = db.query(AdminUser).count()
        if count == 1:
            confirm = input("This is the last admin user. Are you sure? (yes/no): ")
            if confirm.lower() != 'yes':
                print("Cancelled.")
                return

        db.delete(admin)
        db.commit()
        print(f"Admin user '{args.username}' deleted.")
    finally:
        db.close()


def cmd_reset_password(args):
    """Reset an admin user's password."""
    init_db()
    db = SessionLocal()

    try:
        admin = db.query(AdminUser).filter(AdminUser.username == args.username).first()

        if not admin:
            print(f"Error: Admin user '{args.username}' not found.")
            sys.exit(1)

        admin.password_hash = hash_password(args.password or _prompt_password())
        db.commit()
        print(f"Password reset for '{args.username}'.")
    finally:
        db.close()


def _prompt_password() -> str:
    """Prompt for a password so it never lands in shell history or `ps` output."""
    password = getpass.getpass("Password: ")
    if len(password) < 12:
        print("Error: password must be at least 12 characters.")
        sys.exit(1)
    if getpass.getpass("Confirm password: ") != password:
        print("Error: passwords do not match.")
        sys.exit(1)
    return password


def main():
    os.umask(0o077)  # if this command creates the SQLite DB, keep it private
    parser = argparse.ArgumentParser(
        description="iNaturalist Invasives Monitor Management"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # create-admin
    create_parser = subparsers.add_parser("create-admin", help="Create a new admin user")
    create_parser.add_argument("username", help="Admin username")
    create_parser.add_argument("password", nargs="?", help="Admin password (omit to be prompted; recommended)")

    # list-admins
    subparsers.add_parser("list-admins", help="List all admin users")

    # delete-admin
    delete_parser = subparsers.add_parser("delete-admin", help="Delete an admin user")
    delete_parser.add_argument("username", help="Admin username to delete")

    # reset-password
    reset_parser = subparsers.add_parser("reset-password", help="Reset admin password")
    reset_parser.add_argument("username", help="Admin username")
    reset_parser.add_argument("password", nargs="?", help="New password (omit to be prompted; recommended)")

    args = parser.parse_args()

    if args.command == "create-admin":
        cmd_create_admin(args)
    elif args.command == "list-admins":
        cmd_list_admins(args)
    elif args.command == "delete-admin":
        cmd_delete_admin(args)
    elif args.command == "reset-password":
        cmd_reset_password(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
