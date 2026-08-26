"""Configuration loading and dependency wiring (composition root).

Reads non-secret config from config.json (section 19); secrets come from
environment variables. This is the single place adapters are bound to ports.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from adapters.image_sources import (ImageValidator, RSSSource, TempleSource, WebsiteSource,
    MahakalSource, SalangpurSource, IskconBangaloreSource, IskconVrindavanSource,
    IskconTirupatiSource, SwaminarayanSource, MayapurSource)
from adapters.github import GitHubApiRepository
from adapters.page_renderer import PageRenderer
from adapters.repo_sync import RepoSync
from adapters.whatsapp import MetaWhatsAppClient
from application.delivery_service import DeliveryService
from application.image_service import ImageCollector, ImageService
from application.payment_service import PaymentService
from application.renewal_reminder_service import RenewalReminderService
from application.subscriber_service import SubscriberService
from repositories.log_repository import CSVLogRepository
from repositories.payment_repository import CSVPaymentRepository
from repositories.processed_message_repository import CSVProcessedMessageRepository
from repositories.renewal_repository import CSVRenewalRepository
from repositories.sentlog_repository import CSVSentLogRepository
from repositories.subscriber_repository import CSVSubscriberRepository

_DEFAULT_CONFIG_PATH = os.environ.get("DAILY_DARSHAN_CONFIG", "config.json")


@lru_cache(maxsize=None)
def load_config(path: str = _DEFAULT_CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Container:
    """Builds and holds wired services. Construct once per process."""

    def __init__(self, config: dict | None = None, root: str = "."):
        self.config = config or load_config()
        self.root = root
        paths = self.config["paths"]

        def p(rel: str) -> str:
            return os.path.join(root, rel)

        # Repositories
        self.subscribers = CSVSubscriberRepository(p(paths["subscribers_csv"]))
        self.payments = CSVPaymentRepository(p(paths["payments_csv"]))
        self.sentlog = CSVSentLogRepository(p(paths["sentlog_csv"]))
        self.renewals = CSVRenewalRepository(p(paths["renewals_csv"]))
        self.logs = CSVLogRepository(p(paths["logs_csv"]))
        # Webhook idempotency store (fix #1). Default path if not configured.
        self.processed = CSVProcessedMessageRepository(
            p(paths.get("processed_csv", "csv/processed.csv"))
        )

        # WhatsApp app secret for webhook signature verification (fix #2).
        self.whatsapp_app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")

        # WhatsApp adapter (secrets from env)
        self.whatsapp = MetaWhatsAppClient()

        plans = self.config["plans"]

        # Services
        self.payment_service = PaymentService(
            self.payments, plans, self.config["upi"], self.logs
        )
        self.subscriber_service = SubscriberService(
            self.subscribers, self.payments, plans, self.sentlog, self.logs
        )
        delivery_cfg = self.config.get("delivery", {})
        self.delivery_service = DeliveryService(
            self.subscribers,
            self.sentlog,
            self.whatsapp,
            self.subscriber_service,
            caption_template=delivery_cfg.get("caption", "Daily Darshan - {date}"),
            max_retries=delivery_cfg.get("max_send_retries", 3),
            logs=self.logs,
            delivery_mode=delivery_cfg.get("mode", "image"),
            template_name=delivery_cfg.get("template_name", ""),
            template_lang=delivery_cfg.get("template_lang", "en"),
            page_base_url=delivery_cfg.get("page_base_url", ""),
        )
        self.renewal_service = RenewalReminderService(
            self.subscribers,
            self.renewals,
            self.whatsapp,
            reminder_days=self.config.get("renewal", {}).get("reminder_days", [3, 1]),
            logs=self.logs,
        )

        # Image pipeline
        self.image_validator = self._build_validator()
        self.image_collector = ImageCollector(
            self._build_sources(), self.image_validator, self.logs,
            rotation=self.config.get("daily_image_rotation"),
        )
        self.image_service = ImageService(
            self.image_collector, self.image_validator, paths["images_dir"]
        )
        self.images_dir = p(paths["images_dir"])

        # Per-subscriber static page renderer (utility-link target).
        self.page_renderer = PageRenderer(
            pages_dir=delivery_cfg.get("pages_dir", "docs"),
            image_public_base=delivery_cfg.get("image_public_base", ""),
            image_url_path=delivery_cfg.get("image_url_path", "images"),
        )

        # Durable webhook persistence (P0 fix #6): back local CSVs with the
        # shared GitHub repo via the Contents API. Enabled when configured with
        # a token + repo; a no-op otherwise (local/dev, and the scheduler which
        # commits via git directly).
        self.repo_sync = self._build_repo_sync(paths)

    def _build_repo_sync(self, paths: dict) -> RepoSync:
        persistence = self.config.get("persistence", {})
        mode = persistence.get("mode", "local")
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPO", "")
        enabled = mode == "github_api" and bool(token) and bool(repo)
        github = None
        if enabled:
            github = GitHubApiRepository(
                repo=repo,
                branch=persistence.get("branch", "main"),
                token=token,
            )
        # Files the webhook mutates and must share with the scheduler/admin.
        tracked = [
            paths["subscribers_csv"],
            paths["payments_csv"],
            paths.get("processed_csv", "csv/processed.csv"),
            paths["logs_csv"],
        ]
        # Quiet window (UTC) during which the webhook defers pushes so it does
        # not write on top of an in-flight scheduler job. Brackets the image
        # (02:30) through delivery (03:00) jobs; a small margin is added.
        window = persistence.get("quiet_window_utc", {})
        quiet_window = (window.get("start", ""), window.get("end", ""))
        return RepoSync(github, self.root, tracked, enabled, quiet_window=quiet_window)

    def _build_validator(self) -> ImageValidator:
        v = self.config.get("image_validation", {})
        return ImageValidator(
            allowed_formats=v.get("allowed_formats"),
            min_width=v.get("min_width", 0),
            min_height=v.get("min_height", 0),
        )

    def _build_sources(self) -> list | dict:
        cfg = self.config.get("image_source_config", {})
        factories = {
            "temple": lambda: TempleSource(cfg["temple"]["base_url"]),
            "rss": lambda: RSSSource(cfg["rss"]["feed_url"]),
            "website": lambda: WebsiteSource(cfg["website"]["page_url"]),
        }
        temple_factories = {
            "mahakal": MahakalSource, "salangpur": SalangpurSource,
            "iskcon_bangalore": IskconBangaloreSource, "iskcon_vrindavan": IskconVrindavanSource,
            "iskcon_tirupati": IskconTirupatiSource, "swaminarayan": SwaminarayanSource,
            "mayapur": MayapurSource,
        }
        remote_cfg = self.config.get("temple_sources", {})
        if self.config.get("daily_image_rotation"):
            sources = {}
            for name, cls in temple_factories.items():
                item = remote_cfg.get(name, {})
                if item.get("enabled", True) and item.get("page_url"):
                    sources[name] = cls(item["page_url"])
            # Friday's Devi slot remains deliberately pluggable/configured.
            if remote_cfg.get("devi", {}).get("enabled") and remote_cfg["devi"].get("page_url"):
                sources["devi"] = WebsiteSource(remote_cfg["devi"]["page_url"])
            return sources
        sources = []
        for name in self.config.get("image_sources", []):
            if name in factories and name in cfg:
                sources.append(factories[name]())
        return sources
