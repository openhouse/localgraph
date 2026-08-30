from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .automation import (
    configure_google_drive_source,
    configure_instagram_baseline,
    instagram_sync_lock,
    install_daily_import,
    install_instagram_sync,
    run_daily_import,
)
from .drive import DriveAPIError, authenticate_google_drive, configure_google_drive_api, pull_google_drive_folder
from .facebook_accounts import (
    configure_facebook_account,
    exclude_facebook_account,
    facebook_accounts_status,
    verify_facebook_export_capability,
)
from .facebook import scan_facebook_source
from .facebook_sync import configure_facebook_baseline, install_facebook_sync, run_facebook_sync
from .ingest import import_workspace_sources
from .instagram import scan_instagram_source
from .instagram_accounts import configure_instagram_account, instagram_accounts_status
from .imessage_sync import imessage_status, install_imessage_sync, run_imessage_sync
from .paths import Workspace
from .render import render_views
from .schema import connect, initialize_schema
from .status import build_localgraph_status, record_lifecycle_stage
from .twitter_accounts import configure_twitter_account, twitter_accounts_status
from .twitter_sync import install_twitter_sync, run_twitter_sync
from .views import view_kinds, view_path
from .whatsapp import configure_chat, install_whatsapp_sync, record_export, record_acquisition_failure, run_whatsapp_sync
from .whatsapp_acquisition import configure_acquisition, run_acquisition, install_acquisition


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localgraph",
        description="Local-first correspondence graph for private source texts and views.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Localgraph workspace root. Defaults to the current directory.",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    whatsapp = commands.add_parser("configure-whatsapp-chat", help="Explicitly bind one approved private WhatsApp chat.")
    whatsapp.add_argument("--account", required=True)
    whatsapp.add_argument("--chat", required=True)
    whatsapp.add_argument("--title", required=True)
    whatsapp.add_argument("--kind", choices=("direct", "group"), required=True)
    whatsapp.add_argument("--date-order", choices=("mdy", "dmy"), required=True)
    whatsapp.add_argument("--timezone", required=True)
    whatsapp.add_argument("--disabled", action="store_true")
    delivery = commands.add_parser("whatsapp-deliver", help="Validate, copy and bind a completed native or historical chat export.")
    delivery.add_argument("--account", required=True)
    delivery.add_argument("--chat", required=True)
    delivery.add_argument("--archive", type=Path, required=True)
    delivery.add_argument("--observed-title", required=True)
    delivery.add_argument("--exported-at", required=True)
    delivery.add_argument("--origin", choices=("mac-native", "phone-export", "historical-local"), required=True)
    delivery.add_argument("--media-requested", action="store_true")
    failed = commands.add_parser("whatsapp-acquisition-failed", help="Record a body-free native acquisition failure.")
    failed.add_argument("--account", required=True)
    failed.add_argument("--chat", required=True)
    failed.add_argument("--reason", required=True, choices=("session-unavailable", "app-disconnected", "export-control-changed", "export-failed", "identity-unverified"))
    commands.add_parser("whatsapp-sync", help="Import accepted WhatsApp archives and atomically refresh chat views.")
    wa_install = commands.add_parser("install-whatsapp-sync", help="Install the local WhatsApp import watcher; native exports are separate.")
    wa_install.add_argument("--interval-minutes", type=int, default=60)
    wa_install.add_argument("--dry-run", action="store_true")
    wa_native = commands.add_parser("configure-whatsapp-acquisition", help="Authorize native AppleScript discovery and acquisition for all Mac chats.")
    wa_native.add_argument("--account", required=True)
    wa_native.add_argument("--expected-profile", required=True, help="Verified reserved WhatsApp username.")
    wa_native.add_argument("--date-order", choices=("mdy", "dmy"), required=True)
    wa_native.add_argument("--timezone", required=True)
    wa_native.add_argument("--all-chats", action="store_true", required=True)
    wa_acquire = commands.add_parser("whatsapp-acquire", help="Discover and export native chats, validate delivery, import and render.")
    wa_acquire.add_argument("--inventory-only", action="store_true")
    wa_acquire.add_argument("--chat", action="append", help="Restrict acquisition to a local key for acceptance; inventory still covers all chats.")
    wa_acquire.add_argument("--downloads", type=Path)
    wa_discover = commands.add_parser("whatsapp-discover", help="Update population evidence without exporting any chat.")
    wa_discover.add_argument("--if-due", action="store_true", help="Skip a fresh successful discovery of this candidate and policy.")
    wa_refresh = commands.add_parser("whatsapp-refresh", help="Resume bounded per-chat refreshes independently of discovery.")
    wa_refresh.add_argument("--chat", action="append")
    wa_refresh.add_argument("--max-chats", type=int, default=10)
    wa_refresh.add_argument("--force", action="store_true", help="Refresh even recently accepted chats for live acceptance.")
    wa_refresh.add_argument("--downloads", type=Path)
    wa_acq_install = commands.add_parser("install-whatsapp-acquisition", help="Install the verified AppleScript acquisition LaunchAgent.")
    wa_acq_install.add_argument("--hour", type=int, default=9)
    wa_acq_install.add_argument("--dry-run", action="store_true")

    commands.add_parser("plan", help="Print the planned private root and view layout.")

    init = commands.add_parser("init", help="Create workspace directories and SQLite schema.")
    init.add_argument("--force", action="store_true", help="Allow initialization in a non-empty directory.")

    commands.add_parser("doctor", help="Check workspace directories and database schema.")
    commands.add_parser(
        "status",
        help="Report body-free health, scheduler, authorization, and lifecycle state for every source and account.",
    )

    scan = commands.add_parser("scan", help="Detect Instagram transfer exports without reading message bodies.")
    scan.add_argument("--source", help="Instagram source directory. Defaults to sources/instagram.")
    facebook_scan = commands.add_parser(
        "facebook-scan",
        help="Detect Facebook message export packets without reading message bodies.",
    )
    facebook_scan.add_argument("--source", help="Facebook source directory. Defaults to sources/facebook.")

    ingest = commands.add_parser("import", help="Import Instagram and iMessage messages into canonical SQLite state.")
    ingest.add_argument("--instagram-source", help="Instagram source directory. Defaults to sources/instagram.")
    ingest.add_argument("--imessage-db", help="iMessage chat.db path. Defaults to sources/imessage/chat.db.")
    ingest.add_argument("--skip-instagram", action="store_true", help="Do not import Instagram messages.")
    ingest.add_argument("--skip-imessage", action="store_true", help="Do not import iMessage messages.")
    ingest.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    ingest.add_argument(
        "--me-instagram",
        action="append",
        default=[],
        help="Instagram participant name that should map to the self identity. May be repeated.",
    )
    ingest.add_argument(
        "--me-imessage",
        action="append",
        default=[],
        help="iMessage handle, phone, or email that should map to the self identity. May be repeated.",
    )
    ingest.add_argument("--render", action="store_true", help="Render filesystem views after import.")

    configure_drive = commands.add_parser("configure-drive", help="Record the local Google Drive Instagram export folder.")
    configure_drive.add_argument("--instagram-drive-source", required=True, help="Local Google Drive folder containing Meta exports.")

    drive_auth = commands.add_parser("drive-auth", help="Authorize private Google Drive API access for local pulls.")
    drive_auth.add_argument("--client-secrets", required=True, help="Google OAuth desktop client JSON path.")
    drive_auth.add_argument("--token-path", help="Private token output path. Defaults to state/google-drive-token.json.")
    drive_auth.add_argument("--no-open-browser", action="store_true", help="Print the auth URL without opening a browser.")
    drive_auth.add_argument("--port", type=int, default=0, help="Loopback OAuth port. Defaults to any free port.")

    configure_drive_api = commands.add_parser("configure-drive-api", help="Record a Google Drive folder ID for authenticated pulls.")
    configure_drive_api.add_argument("--folder-id", required=True, help="Google Drive folder ID containing Meta Instagram exports.")
    configure_drive_api.add_argument("--cache-dir", help="Private local cache path. Defaults to sources/instagram-drive-cache.")
    configure_drive_api.add_argument("--token-path", help="Private OAuth token path. Defaults to state/google-drive-token.json.")

    configure_baseline = commands.add_parser(
        "configure-instagram-baseline",
        help="Record a verified one-time all-history Instagram export as the cumulative baseline.",
    )
    configure_baseline.add_argument(
        "--export-name",
        required=True,
        help="Exact completed instagram-* folder name created by the verified all-history export.",
    )
    configure_baseline.add_argument(
        "--account",
        help="Configured Instagram account key. Omit for a legacy singleton workspace.",
    )

    configure_account = commands.add_parser(
        "configure-instagram-account",
        help="Add or update one account in the multi-account Instagram registry.",
    )
    configure_account.add_argument("--account", required=True, help="Stable local account key, usually the username.")
    configure_account.add_argument("--profile-name", required=True, help="Instagram profile username without @.")
    configure_account.add_argument("--owner-display-name", required=True, help="Display name for the exporting identity.")
    configure_account.add_argument("--owner-kind", choices=("person", "organization"), required=True)
    configure_account.add_argument("--self-name", action="append", default=[], help="Participant name belonging to this account. May be repeated.")
    configure_account.add_argument("--export-name-prefix", help="Exact Meta export folder prefix. Defaults to instagram-<profile>-." )
    configure_account.add_argument("--adopt-legacy", action="store_true", help="Reuse the existing singleton cache, registry, mirror, and status paths.")
    configure_account.add_argument("--reuse-primary-drive", action="store_true", help="Reuse the primary account's Drive folder and read-only OAuth token.")
    configure_account.add_argument("--primary", action="store_true", help="Make this the primary Instagram account.")
    configure_account.add_argument("--disabled", action="store_true", help="Register a pending account visibly without blocking synchronization of active accounts.")

    commands.add_parser(
        "instagram-accounts",
        help="List configured Instagram accounts and body-free sync health.",
    )

    configure_facebook = commands.add_parser(
        "configure-facebook-account",
        help="Add or update one Facebook profile or managed Page in the private account registry.",
    )
    configure_facebook.add_argument("--account", required=True, help="Stable local account key.")
    configure_facebook.add_argument("--display-name", required=True, help="Facebook profile or Page name.")
    configure_facebook.add_argument("--account-type", choices=("profile", "page"), required=True)
    configure_facebook.add_argument(
        "--provider-state",
        choices=("active", "deactivated", "unknown"),
        default="active",
    )
    configure_facebook.add_argument("--self-name", action="append", default=[], help="Export participant name belonging to this account. May be repeated.")
    configure_facebook.add_argument("--export-name-prefix", help="Exact provider export folder prefix. Defaults to facebook-<account>-.")
    configure_facebook.add_argument("--reuse-instagram-drive", action="store_true", help="Reuse the configured Instagram Drive container and read-only token.")
    configure_facebook.add_argument("--disabled", action="store_true", help="Keep the record visible but skip automated imports.")

    exclude_facebook = commands.add_parser(
        "exclude-facebook-account",
        help="Permanently remove an account from synchronization and block re-enrollment.",
    )
    exclude_facebook.add_argument("--account", required=True, help="Stable local account key.")
    exclude_facebook.add_argument("--reason", default="privacy-exclusion", help="Private exclusion reason code.")

    verify_facebook = commands.add_parser(
        "verify-facebook-export-capability",
        help="Record an individual Facebook Page export capability observation before onboarding.",
    )
    verify_facebook.add_argument("--account", required=True)
    verify_facebook.add_argument("--capability", choices=("supported", "unsupported"), required=True)
    verify_facebook.add_argument("--provider-surface", required=True)
    verify_facebook.add_argument("--observed-at", required=True, help="Timezone-qualified ISO 8601 observation time.")

    lifecycle = commands.add_parser(
        "record-lifecycle",
        help="Record a provider-observed requested or preparing lifecycle event.",
    )
    lifecycle.add_argument("--source", choices=("instagram", "facebook", "twitter"), required=True)
    lifecycle.add_argument("--account", required=True)
    lifecycle.add_argument("--stage", choices=("requested", "preparing"), required=True)
    lifecycle.add_argument(
        "--evidence",
        choices=("provider-activity-record", "operator-observed-provider-ui"),
        required=True,
    )
    lifecycle.add_argument("--observed-at", required=True, help="Timezone-qualified ISO 8601 observation time.")

    commands.add_parser(
        "facebook-accounts",
        help="List configured Facebook profiles and Pages with body-free sync health.",
    )
    configure_twitter = commands.add_parser(
        "configure-twitter-account",
        help="Add or update one X/Twitter account in the private archive registry.",
    )
    configure_twitter.add_argument("--account", required=True, help="Stable X/Twitter username without @.")
    configure_twitter.add_argument("--display-name", required=True)
    configure_twitter.add_argument("--owner-kind", choices=("person", "organization"), required=True)
    configure_twitter.add_argument("--self-name", action="append", default=[])
    configure_twitter.add_argument("--disabled", action="store_true")
    commands.add_parser("twitter-accounts", help="List configured X/Twitter accounts and body-free archive health.")
    twitter_sync = commands.add_parser(
        "twitter-sync",
        help="Import account-scoped X/Twitter archives and refresh canonical views.",
    )
    twitter_sync.add_argument("--no-render", action="store_true")
    install_twitter = commands.add_parser("install-twitter-sync", help="Install an hourly local X/Twitter archive check.")
    install_twitter.add_argument("--interval-minutes", type=int, default=60)
    install_twitter.add_argument("--label", default="com.openhouse.localgraph.twitter-sync")
    install_twitter.add_argument("--dry-run", action="store_true")
    configure_facebook_baseline_parser = commands.add_parser(
        "configure-facebook-baseline",
        help="Record a verified one-time all-history Facebook Messages export for one account.",
    )
    configure_facebook_baseline_parser.add_argument("--account", required=True)
    configure_facebook_baseline_parser.add_argument("--export-name", required=True)
    facebook_sync = commands.add_parser(
        "facebook-sync",
        help="Import materialized Facebook profile and Page message packets and refresh canonical views.",
    )
    facebook_sync.add_argument("--no-render", action="store_true", help="Do not render views after import.")

    install_facebook = commands.add_parser(
        "install-facebook-sync",
        help="Install an hourly macOS Facebook message sync LaunchAgent.",
    )
    install_facebook.add_argument("--interval-minutes", type=int, default=60)
    install_facebook.add_argument("--label", default="com.openhouse.localgraph.facebook-sync")
    install_facebook.add_argument("--dry-run", action="store_true")

    imessage_sync = commands.add_parser(
        "imessage-sync",
        help="Snapshot the live macOS Messages database and atomically refresh canonical iMessage state.",
    )
    imessage_sync.add_argument("--source-db", help="Live chat.db path. Defaults to ~/Library/Messages/chat.db.")
    imessage_sync.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    imessage_sync.add_argument("--me-imessage", action="append", default=[], help="Self handle. May be repeated.")
    imessage_sync.add_argument("--no-render", action="store_true", help="Do not render views after import.")

    commands.add_parser("imessage-status", help="Report body-free Apple Messages freshness and health.")

    install_imessage = commands.add_parser(
        "install-imessage-sync",
        help="Install an hourly macOS Apple Messages sync LaunchAgent.",
    )
    install_imessage.add_argument("--source-db", help="Live chat.db path. Defaults to ~/Library/Messages/chat.db.")
    install_imessage.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    install_imessage.add_argument("--interval-minutes", type=int, default=60)
    install_imessage.add_argument("--label", default="com.openhouse.localgraph.imessage-sync")
    install_imessage.add_argument("--dry-run", action="store_true")

    drive_pull = commands.add_parser("drive-pull", help="Pull a private Google Drive folder into the local Instagram cache.")
    drive_pull.add_argument("--folder-id", help="Google Drive folder ID. Defaults to configured imports.instagram.googleDriveFolderId.")
    drive_pull.add_argument("--cache-dir", help="Private local cache path. Defaults to configured cache path.")
    drive_pull.add_argument("--token-path", help="Private OAuth token path. Defaults to configured token path.")

    daily_import = commands.add_parser("daily-import", help="Import the daily Instagram export from Google Drive.")
    daily_import.add_argument("--instagram-drive-source", help="Local Google Drive Instagram folder. Overrides config/discovery.")
    daily_import.add_argument("--imessage-db", help="Optional iMessage chat.db path. Defaults to sources/imessage/chat.db.")
    daily_import.add_argument("--skip-instagram", action="store_true", help="Do not import Instagram messages.")
    daily_import.add_argument("--skip-imessage", action="store_true", help="Do not import iMessage messages.")
    daily_import.add_argument("--no-render", action="store_true", help="Do not render views after import.")
    daily_import.add_argument("--write-config", action="store_true", help="Persist an explicit or discovered Google Drive source.")
    daily_import.add_argument("--all-instagram-exports", action="store_true", help="Import every export under the Drive source instead of only the newest export.")
    daily_import.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    daily_import.add_argument("--me-instagram", action="append", default=[], help="Instagram self name. May be repeated.")
    daily_import.add_argument("--me-imessage", action="append", default=[], help="iMessage self handle. May be repeated.")

    instagram_sync = commands.add_parser("instagram-sync", help="Refresh the current Instagram mirror and canonical views.")
    instagram_sync.add_argument("--no-render", action="store_true", help="Do not render views after import.")
    instagram_sync.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    instagram_sync.add_argument("--me-instagram", action="append", default=[], help="Instagram self name. May be repeated.")

    install_daily = commands.add_parser("install-daily-import", help="Install a macOS LaunchAgent for daily imports.")
    install_daily.add_argument("--instagram-drive-source", help="Local Google Drive Instagram folder to pin in the daily job.")
    install_daily.add_argument("--skip-imessage", action="store_true", help="Do not import iMessage from the daily job.")
    install_daily.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    install_daily.add_argument("--me-instagram", action="append", default=[], help="Instagram self name. May be repeated.")
    install_daily.add_argument("--me-imessage", action="append", default=[], help="iMessage self handle. May be repeated.")
    install_daily.add_argument("--hour", type=int, default=3, help="LaunchAgent hour, 0-23. Defaults to 3.")
    install_daily.add_argument("--minute", type=int, default=15, help="LaunchAgent minute, 0-59. Defaults to 15.")
    install_daily.add_argument("--label", default="com.openhouse.localgraph.daily-import", help="LaunchAgent label.")
    install_daily.add_argument("--dry-run", action="store_true", help="Print planned paths without writing files.")

    install_sync = commands.add_parser("install-instagram-sync", help="Install an hourly macOS Instagram sync LaunchAgent.")
    install_sync.add_argument("--me", default="Me", help="Display name for your own identity. Defaults to 'Me'.")
    install_sync.add_argument("--me-instagram", action="append", default=[], help="Instagram self name. May be repeated.")
    install_sync.add_argument("--interval-minutes", type=int, default=60, help="Provider check interval, 5-1440 minutes. Defaults to 60.")
    install_sync.add_argument("--label", default="com.openhouse.localgraph.instagram-sync", help="LaunchAgent label.")
    install_sync.add_argument("--dry-run", action="store_true", help="Print planned paths without writing files.")

    render = commands.add_parser("render", help="Render filesystem views from canonical SQLite state.")
    render.add_argument("--source", help="Optionally scan an Instagram source directory and include it in the render manifest.")

    view_name = commands.add_parser("view-name", help="Print a deterministic symlink-friendly view path.")
    view_name.add_argument("kind", choices=view_kinds())
    view_name.add_argument("label")
    view_name.add_argument("source_key")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Workspace(Path(args.root).expanduser().resolve())

    try:
        if args.command == "plan":
            summary = command_plan(workspace)
        elif args.command == "init":
            summary = command_init(workspace, force=args.force)
        elif args.command == "doctor":
            summary = command_doctor(workspace)
        elif args.command == "status":
            summary = build_localgraph_status(workspace)
        elif args.command == "scan":
            summary = command_scan(workspace, source=args.source)
        elif args.command == "facebook-scan":
            source_path = Path(args.source).expanduser() if args.source else workspace.facebook_source_dir
            if not source_path.is_absolute():
                source_path = workspace.root / source_path
            summary = scan_facebook_source(source_path)
        elif args.command == "import":
            summary = command_import(
                workspace,
                instagram_source=args.instagram_source,
                imessage_db=args.imessage_db,
                skip_instagram=args.skip_instagram,
                skip_imessage=args.skip_imessage,
                me=args.me,
                me_instagram=args.me_instagram,
                me_imessage=args.me_imessage,
                render=args.render,
            )
        elif args.command == "configure-drive":
            summary = command_configure_drive(workspace, instagram_drive_source=args.instagram_drive_source)
        elif args.command == "drive-auth":
            summary = command_drive_auth(
                workspace,
                client_secrets=args.client_secrets,
                token_path=args.token_path,
                open_browser=not args.no_open_browser,
                port=args.port,
            )
        elif args.command == "configure-drive-api":
            summary = command_configure_drive_api(
                workspace,
                folder_id=args.folder_id,
                cache_dir=args.cache_dir,
                token_path=args.token_path,
            )
        elif args.command == "configure-instagram-baseline":
            summary = configure_instagram_baseline(workspace, args.export_name, account_key=args.account)
        elif args.command == "configure-instagram-account":
            summary = configure_instagram_account(
                workspace,
                account_key=args.account,
                profile_name=args.profile_name,
                owner_display_name=args.owner_display_name,
                owner_kind=args.owner_kind,
                self_names=args.self_name,
                export_name_prefix=args.export_name_prefix,
                adopt_legacy=args.adopt_legacy,
                reuse_primary_drive=args.reuse_primary_drive,
                primary=args.primary,
                enabled=not args.disabled,
            )
        elif args.command == "instagram-accounts":
            summary = instagram_accounts_status(workspace)
        elif args.command == "configure-facebook-account":
            summary = configure_facebook_account(
                workspace,
                account_key=args.account,
                display_name=args.display_name,
                account_type=args.account_type,
                provider_state=args.provider_state,
                self_names=args.self_name,
                export_name_prefix=args.export_name_prefix,
                reuse_instagram_drive=args.reuse_instagram_drive,
                enabled=not args.disabled,
            )
        elif args.command == "exclude-facebook-account":
            summary = exclude_facebook_account(
                workspace,
                account_key=args.account,
                reason=args.reason,
            )
        elif args.command == "verify-facebook-export-capability":
            summary = verify_facebook_export_capability(
                workspace,
                account_key=args.account,
                capability=args.capability,
                provider_surface=args.provider_surface,
                observed_at=args.observed_at,
            )
        elif args.command == "record-lifecycle":
            summary = record_lifecycle_stage(
                workspace,
                source=args.source,
                account=args.account,
                stage=args.stage,
                observed_at=args.observed_at,
                evidence=args.evidence,
            )
        elif args.command == "facebook-accounts":
            summary = facebook_accounts_status(workspace)
        elif args.command == "configure-whatsapp-chat":
            summary = configure_chat(workspace, account_key=args.account, chat_key=args.chat, title=args.title,
                                     kind=args.kind, date_order=args.date_order, timezone_name=args.timezone,
                                     enabled=not args.disabled)
        elif args.command == "whatsapp-deliver":
            summary = record_export(workspace, account_key=args.account, chat_key=args.chat, archive=args.archive,
                                    observed_title=args.observed_title, exported_at=args.exported_at,
                                    media_requested=args.media_requested, origin=args.origin)
        elif args.command == "whatsapp-acquisition-failed":
            summary = record_acquisition_failure(workspace, account_key=args.account, chat_key=args.chat, reason=args.reason)
        elif args.command == "whatsapp-sync":
            summary = run_whatsapp_sync(workspace)
            if summary["status"] == "degraded":
                print(json.dumps(summary, indent=2, sort_keys=True))
                return 1
        elif args.command == "install-whatsapp-sync":
            summary = install_whatsapp_sync(workspace, interval_minutes=args.interval_minutes, dry_run=args.dry_run)
        elif args.command == "configure-whatsapp-acquisition":
            summary = configure_acquisition(workspace, account=args.account, expected_profile=args.expected_profile,
                                            date_order=args.date_order, timezone_name=args.timezone)
        elif args.command in {"whatsapp-acquire", "whatsapp-discover", "whatsapp-refresh"}:
            summary = run_acquisition(workspace, downloads=getattr(args, "downloads", None),
                inventory_only=args.command == "whatsapp-discover" or getattr(args, "inventory_only", False),
                chat_keys=getattr(args, "chat", None), refresh_only=args.command == "whatsapp-refresh",
                resume=args.command == "whatsapp-refresh" and not args.force, max_chats=getattr(args, "max_chats", None),
                discovery_if_due=getattr(args, "if_due", False))
            failed = (summary.get("refreshStatus") == "degraded" or bool(summary.get("error"))
                      if args.command == "whatsapp-refresh" else summary["status"] == "degraded")
            if failed:
                print(json.dumps(summary, indent=2, sort_keys=True))
                return 1
        elif args.command == "install-whatsapp-acquisition":
            summary = install_acquisition(workspace, hour=args.hour, dry_run=args.dry_run)
        elif args.command == "configure-twitter-account":
            summary = configure_twitter_account(
                workspace,
                account_key=args.account,
                display_name=args.display_name,
                owner_kind=args.owner_kind,
                self_names=args.self_name,
                enabled=not args.disabled,
            )
        elif args.command == "twitter-accounts":
            summary = twitter_accounts_status(workspace)
        elif args.command == "twitter-sync":
            with instagram_sync_lock(workspace) as acquired:
                if not acquired:
                    summary = {
                        "workspace": str(workspace.root),
                        "twitterSync": {
                            "schemaVersion": 1,
                            "status": "skipped-concurrent",
                            "lockPath": str(workspace.state_dir / "instagram-sync.lock"),
                        },
                    }
                else:
                    summary = run_twitter_sync(workspace, render=not args.no_render)
        elif args.command == "install-twitter-sync":
            summary = install_twitter_sync(
                workspace,
                interval_minutes=args.interval_minutes,
                label=args.label,
                dry_run=args.dry_run,
            )
        elif args.command == "configure-facebook-baseline":
            summary = configure_facebook_baseline(
                workspace,
                account_key=args.account,
                export_name=args.export_name,
            )
        elif args.command == "facebook-sync":
            with instagram_sync_lock(workspace) as acquired:
                if not acquired:
                    summary = {
                        "workspace": str(workspace.root),
                        "facebookSync": {
                            "schemaVersion": 1,
                            "status": "skipped-concurrent",
                            "lockPath": str(workspace.state_dir / "instagram-sync.lock"),
                        },
                    }
                else:
                    summary = run_facebook_sync(workspace, render=not args.no_render)
        elif args.command == "install-facebook-sync":
            summary = install_facebook_sync(
                workspace,
                interval_minutes=args.interval_minutes,
                label=args.label,
                dry_run=args.dry_run,
            )
        elif args.command == "imessage-sync":
            with instagram_sync_lock(workspace) as acquired:
                if not acquired:
                    summary = {
                        "workspace": str(workspace.root),
                        "imessageSync": {
                            "schemaVersion": 1,
                            "status": "skipped-concurrent",
                            "lockPath": str(workspace.state_dir / "instagram-sync.lock"),
                        },
                    }
                else:
                    summary = run_imessage_sync(
                        workspace,
                        live_db_path=Path(args.source_db) if args.source_db else None,
                        me_name=args.me,
                        me_handles=args.me_imessage,
                        render=not args.no_render,
                    )
        elif args.command == "imessage-status":
            summary = imessage_status(workspace)
        elif args.command == "install-imessage-sync":
            summary = install_imessage_sync(
                workspace,
                interval_minutes=args.interval_minutes,
                label=args.label,
                live_db_path=Path(args.source_db) if args.source_db else None,
                me_name=args.me,
                dry_run=args.dry_run,
            )
        elif args.command == "drive-pull":
            summary = command_drive_pull(
                workspace,
                folder_id=args.folder_id,
                cache_dir=args.cache_dir,
                token_path=args.token_path,
            )
        elif args.command == "daily-import":
            summary = command_daily_import(
                workspace,
                instagram_drive_source=args.instagram_drive_source,
                imessage_db=args.imessage_db,
                skip_instagram=args.skip_instagram,
                skip_imessage=args.skip_imessage,
                render=not args.no_render,
                write_config=args.write_config,
                latest_instagram_only=not args.all_instagram_exports,
                replace_instagram_snapshot=False,
                me=args.me,
                me_instagram=args.me_instagram,
                me_imessage=args.me_imessage,
            )
        elif args.command == "instagram-sync":
            with instagram_sync_lock(workspace) as acquired:
                if not acquired:
                    summary = {
                        "workspace": str(workspace.root),
                        "instagramSync": {
                            "schemaVersion": 1,
                            "status": "skipped-concurrent",
                            "lockPath": str(workspace.state_dir / "instagram-sync.lock"),
                        },
                    }
                else:
                    summary = command_daily_import(
                        workspace,
                        instagram_drive_source=None,
                        imessage_db=None,
                        skip_instagram=False,
                        skip_imessage=True,
                        render=not args.no_render,
                        write_config=False,
                        latest_instagram_only=True,
                        replace_instagram_snapshot=True,
                        me=args.me,
                        me_instagram=args.me_instagram,
                        me_imessage=[],
                    )
        elif args.command == "install-daily-import":
            summary = command_install_daily_import(
                workspace,
                instagram_drive_source=args.instagram_drive_source,
                skip_imessage=args.skip_imessage,
                me=args.me,
                me_instagram=args.me_instagram,
                me_imessage=args.me_imessage,
                hour=args.hour,
                minute=args.minute,
                label=args.label,
                dry_run=args.dry_run,
            )
        elif args.command == "install-instagram-sync":
            summary = command_install_instagram_sync(
                workspace,
                me=args.me,
                me_instagram=args.me_instagram,
                interval_minutes=args.interval_minutes,
                label=args.label,
                dry_run=args.dry_run,
            )
        elif args.command == "render":
            summary = command_render(workspace, source=args.source)
        elif args.command == "view-name":
            summary = command_view_name(workspace, args.kind, args.label, args.source_key)
        else:
            parser.error(f"unknown command: {args.command}")
    except (DriveAPIError, FileNotFoundError, LocalgraphError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def command_plan(workspace: Workspace) -> dict[str, object]:
    return workspace.plan()


def command_init(workspace: Workspace, *, force: bool = False) -> dict[str, object]:
    workspace.ensure_workspace(force=force)
    with connect(workspace.database_path) as db:
        initialize_schema(db)
    return {
        "root": str(workspace.root),
        "database": str(workspace.database_path),
        "config": str(workspace.config_path),
        "created": [str(path) for path in workspace.managed_directories],
        "views": [str(path) for path in workspace.view_directories],
    }


def command_doctor(workspace: Workspace) -> dict[str, object]:
    checks = workspace.check()
    schema_ok = False
    if workspace.database_path.exists():
        with connect(workspace.database_path) as db:
            schema_ok = bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'").fetchone())
    return {
        "root": str(workspace.root),
        "directories": checks,
        "database": "ok" if schema_ok else "missing schema",
    }


def command_scan(workspace: Workspace, *, source: str | None) -> dict[str, object]:
    source_path = Path(source).expanduser() if source else workspace.instagram_source_dir
    if not source_path.is_absolute():
        source_path = workspace.root / source_path
    return scan_instagram_source(source_path)


def command_import(
    workspace: Workspace,
    *,
    instagram_source: str | None,
    imessage_db: str | None,
    skip_instagram: bool,
    skip_imessage: bool,
    me: str,
    me_instagram: list[str],
    me_imessage: list[str],
    render: bool,
) -> dict[str, object]:
    workspace.ensure_workspace(force=False)
    instagram_path = _resolve_workspace_path(workspace, instagram_source) if instagram_source else workspace.instagram_source_dir
    imessage_path = _resolve_workspace_path(workspace, imessage_db) if imessage_db else workspace.imessage_chat_db_path
    with connect(workspace.database_path) as db:
        initialize_schema(db)
        result = import_workspace_sources(
            db,
            workspace,
            instagram_source=instagram_path,
            imessage_db=imessage_path,
            import_instagram=not skip_instagram,
            import_imessage=not skip_imessage,
            me_name=me,
            me_instagram_names=me_instagram,
            me_imessage_handles=me_imessage,
            explicit_instagram=instagram_source is not None and not skip_instagram,
            explicit_imessage=imessage_db is not None and not skip_imessage,
        )
        if render:
            result["render"] = render_views(db, workspace, source_scan=scan_instagram_source(instagram_path))
    return result


def command_configure_drive(workspace: Workspace, *, instagram_drive_source: str) -> dict[str, object]:
    return configure_google_drive_source(workspace, _resolve_workspace_path(workspace, instagram_drive_source))


def command_drive_auth(
    workspace: Workspace,
    *,
    client_secrets: str,
    token_path: str | None,
    open_browser: bool,
    port: int,
) -> dict[str, object]:
    return authenticate_google_drive(
        workspace,
        client_secrets_path=_resolve_workspace_path(workspace, client_secrets),
        token_path=_resolve_workspace_path(workspace, token_path) if token_path else None,
        open_browser=open_browser,
        port=port,
    )


def command_configure_drive_api(
    workspace: Workspace,
    *,
    folder_id: str,
    cache_dir: str | None,
    token_path: str | None,
) -> dict[str, object]:
    return configure_google_drive_api(
        workspace,
        folder_id=folder_id,
        cache_dir=_resolve_workspace_path(workspace, cache_dir) if cache_dir else None,
        token_path=_resolve_workspace_path(workspace, token_path) if token_path else None,
    )


def command_drive_pull(
    workspace: Workspace,
    *,
    folder_id: str | None,
    cache_dir: str | None,
    token_path: str | None,
) -> dict[str, object]:
    if folder_id is None:
        config = json.loads(workspace.config_path.read_text(encoding="utf-8")) if workspace.config_path.exists() else {}
        folder_id = (
            config.get("imports", {})
            .get("instagram", {})
            .get("googleDriveFolderId")
        )
    if not folder_id:
        raise LocalgraphError("no Google Drive folder ID configured; run configure-drive-api --folder-id <id>")
    return pull_google_drive_folder(
        workspace,
        folder_id=str(folder_id),
        cache_dir=_resolve_workspace_path(workspace, cache_dir) if cache_dir else None,
        token_path=_resolve_workspace_path(workspace, token_path) if token_path else None,
    ).to_json()


def command_daily_import(
    workspace: Workspace,
    *,
    instagram_drive_source: str | None,
    imessage_db: str | None,
    skip_instagram: bool,
    skip_imessage: bool,
    render: bool,
    write_config: bool,
    latest_instagram_only: bool,
    replace_instagram_snapshot: bool,
    me: str,
    me_instagram: list[str],
    me_imessage: list[str],
) -> dict[str, object]:
    return run_daily_import(
        workspace,
        instagram_drive_source=_resolve_workspace_path(workspace, instagram_drive_source) if instagram_drive_source else None,
        imessage_db=_resolve_workspace_path(workspace, imessage_db) if imessage_db else None,
        me_name=me,
        me_instagram_names=me_instagram,
        me_imessage_handles=me_imessage,
        skip_instagram=skip_instagram,
        skip_imessage=skip_imessage,
        render=render,
        write_config_on_discovery=write_config,
        latest_instagram_only=latest_instagram_only,
        replace_instagram_snapshot=replace_instagram_snapshot,
    )


def command_install_daily_import(
    workspace: Workspace,
    *,
    instagram_drive_source: str | None,
    skip_imessage: bool,
    me: str,
    me_instagram: list[str],
    me_imessage: list[str],
    hour: int,
    minute: int,
    label: str,
    dry_run: bool,
) -> dict[str, object]:
    return install_daily_import(
        workspace,
        instagram_drive_source=_resolve_workspace_path(workspace, instagram_drive_source) if instagram_drive_source else None,
        skip_imessage=skip_imessage,
        me_name=me,
        me_instagram_names=me_instagram,
        me_imessage_handles=me_imessage,
        hour=hour,
        minute=minute,
        label=label,
        dry_run=dry_run,
    )


def command_install_instagram_sync(
    workspace: Workspace,
    *,
    me: str,
    me_instagram: list[str],
    interval_minutes: int,
    label: str,
    dry_run: bool,
) -> dict[str, object]:
    return install_instagram_sync(
        workspace,
        me_name=me,
        me_instagram_names=me_instagram,
        interval_minutes=interval_minutes,
        label=label,
        dry_run=dry_run,
    )


def command_render(workspace: Workspace, *, source: str | None = None) -> dict[str, object]:
    if not workspace.database_path.exists():
        raise LocalgraphError(f"database does not exist: {workspace.database_path}")
    source_scan = command_scan(workspace, source=source)
    with connect(workspace.database_path) as db:
        result = render_views(db, workspace, source_scan=source_scan)
    return result


def command_view_name(workspace: Workspace, kind: str, label: str, source_key: str) -> dict[str, object]:
    path = view_path(workspace.root, kind, label, source_key)
    return {
        "kind": kind,
        "label": label,
        "sourceKey": source_key,
        "path": str(path),
    }


def _resolve_workspace_path(workspace: Workspace, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace.root / path


class LocalgraphError(Exception):
    """User-facing command error."""


if __name__ == "__main__":
    raise SystemExit(main())
