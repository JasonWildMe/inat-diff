#!/usr/bin/env python3
"""
Management commands for iNaturalist Invasives Monitor.

Usage:
    python -m webapp.manage create-admin <username>      (prompts for password)
    python -m webapp.manage list-admins
    python -m webapp.manage delete-admin <username>
    python -m webapp.manage reset-password <username>    (prompts for password)
    python -m webapp.manage add-species <inat_taxon_id> [--gbif-key N] [--eddmaps-subject N]
    python -m webapp.manage list-species
    python -m webapp.manage sync-species <inat_taxon_id>    (queue rebuilds now)
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


def cmd_add_species(args):
    """Track a species' spread: resolve its GBIF and EDDMapS ids and create sync rows."""
    from inat_diff import iNatClient
    from webapp.models import TrackedSpecies, SourceSync, SpreadSource
    from webapp import spread_sources

    init_db()
    db = SessionLocal()
    try:
        if db.query(TrackedSpecies).filter_by(inat_taxon_id=args.taxon_id).first():
            print(f"Taxon {args.taxon_id} is already tracked.")
            return
        results = iNatClient()._make_request(f"taxa/{args.taxon_id}").get("results") or []
        if not results:
            print(f"Error: iNaturalist taxon {args.taxon_id} not found.")
            sys.exit(1)
        taxon = results[0]
        name = taxon.get("name")
        print(f"iNaturalist taxon {args.taxon_id}: {name} ({taxon.get('preferred_common_name') or 'no common name'}), rank {taxon.get('rank')}")

        gbif = args.gbif_key if args.gbif_key is not None else spread_sources.resolve_gbif_key(name)
        edd = args.eddmaps_subject if args.eddmaps_subject is not None else spread_sources.resolve_eddmaps_subject(name)
        print(f"  GBIF taxon key: {gbif if gbif is not None else 'not found (pass --gbif-key)'}")
        print(f"  EDDMapS subject: {edd if edd is not None else 'not found (pass --eddmaps-subject)'}")

        species = TrackedSpecies(inat_taxon_id=args.taxon_id, gbif_taxon_key=gbif, eddmaps_subject_id=edd,
                                 scientific_name=name, common_name=taxon.get("preferred_common_name"))
        species.syncs = [SourceSync(source=s) for s in SpreadSource]
        db.add(species)
        db.commit()
        print("Tracked. The nightly dispatcher will build it, or run: "
              f"python -m webapp.manage sync-species {args.taxon_id}")
    finally:
        db.close()


def cmd_list_species(args):
    """List tracked species and the state of each source."""
    from webapp.models import TrackedSpecies

    init_db()
    db = SessionLocal()
    try:
        for sp in db.query(TrackedSpecies).order_by(TrackedSpecies.scientific_name):
            print(f"{sp.inat_taxon_id:>9}  {sp.scientific_name} ({sp.common_name or '-'})"
                  f"{'' if sp.is_active else '  [inactive]'}")
            for s in sorted(sp.syncs, key=lambda s: s.source.value):
                ok = s.last_success_at.strftime('%Y-%m-%d %H:%M') if s.last_success_at else 'never'
                err = f"  error: {s.error_message[:80]}" if s.error_message else ""
                print(f"           {s.source.value:<8} {s.status.value:<8} last ok {ok}  "
                      f"counties {s.counties if s.counties is not None else '-'}{err}")
    finally:
        db.close()


def cmd_sync_species(args):
    """Queue an immediate rebuild of every linked source for one species."""
    from webapp.models import TrackedSpecies
    from webapp.spread_tasks import sync_species_source

    init_db()
    db = SessionLocal()
    try:
        sp = db.query(TrackedSpecies).filter_by(inat_taxon_id=args.taxon_id).first()
        if not sp:
            print(f"Error: taxon {args.taxon_id} is not tracked (use add-species).")
            sys.exit(1)
        for s in sp.syncs:
            if sp.source_id(s.source) is not None:
                sync_species_source.delay(sp.id, s.source.value, force=True)
                print(f"Queued {s.source.value}")
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

    add_sp = subparsers.add_parser("add-species", help="Track a species' county spread")
    add_sp.add_argument("taxon_id", type=int, help="iNaturalist taxon id")
    add_sp.add_argument("--gbif-key", type=int, help="GBIF taxon key (skips lookup)")
    add_sp.add_argument("--eddmaps-subject", type=int, help="EDDMapS subject id (skips lookup)")
    subparsers.add_parser("list-species", help="List tracked species and sync state")
    sync_sp = subparsers.add_parser("sync-species", help="Queue spread rebuilds for a species now")
    sync_sp.add_argument("taxon_id", type=int, help="iNaturalist taxon id")

    args = parser.parse_args()

    if args.command == "create-admin":
        cmd_create_admin(args)
    elif args.command == "list-admins":
        cmd_list_admins(args)
    elif args.command == "delete-admin":
        cmd_delete_admin(args)
    elif args.command == "reset-password":
        cmd_reset_password(args)
    elif args.command == "add-species":
        cmd_add_species(args)
    elif args.command == "list-species":
        cmd_list_species(args)
    elif args.command == "sync-species":
        cmd_sync_species(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
