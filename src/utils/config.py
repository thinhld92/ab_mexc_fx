"""Config loader with hot-reload support."""

from __future__ import annotations

import os

import orjson


class Config:
    """
    Load ``config.json`` and expose a small helper API.

    The v2 exchange abstraction expects a generic ``exchange`` section, while the
    current repo still uses the legacy ``mexc`` section. This loader supports both.
    """

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            config_path = os.path.join(project_root, "config.json")

        self.config_path = config_path
        self._last_modified = 0.0
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        with open(self.config_path, "rb") as f:
            self._data = orjson.loads(f.read())
        self._last_modified = os.path.getmtime(self.config_path)

    def reload_if_changed(self) -> bool:
        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime > self._last_modified:
                self._load()
                return True
        except OSError:
            pass
        return False

    @staticmethod
    def _merge_dicts(base: dict, overlay: dict) -> dict:
        merged = dict(base)
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = Config._merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    @property
    def selected_product(self) -> str | None:
        raw = str(self._data.get("product", "")).strip()
        if not raw:
            return None
        return raw.lower()

    @property
    def products(self) -> dict:
        products = self._data.get("products", {})
        return products if isinstance(products, dict) else {}

    @property
    def product_profile(self) -> dict:
        selected = self.selected_product
        if selected is None:
            return {}

        profiles = self.products
        if not profiles:
            raise ValueError(f"Config product '{selected}' requires a 'products' map")

        profile = profiles.get(selected)
        if not isinstance(profile, dict):
            available = ", ".join(sorted(str(name) for name in profiles.keys())) or "<none>"
            raise ValueError(f"Unknown product '{selected}' in config. Available: {available}")
        return profile

    @property
    def product_label(self) -> str:
        profile = self.product_profile
        label = str(profile.get("label", "")).strip()
        if label:
            return label
        selected = self.selected_product
        if selected:
            return selected.upper()
        return "DEFAULT"

    def _merged_section(self, name: str, *, aliases: tuple[str, ...] = ()) -> dict:
        base = {}
        for key in (name, *aliases):
            raw = self._data.get(key)
            if isinstance(raw, dict):
                base = dict(raw)
                break

        overlay = {}
        profile = self.product_profile
        for key in (name, *aliases):
            raw = profile.get(key)
            if isinstance(raw, dict):
                overlay = dict(raw)
                break

        return self._merge_dicts(base, overlay)

    @property
    def redis(self) -> dict:
        return self._merged_section("redis")

    @property
    def vps_name(self) -> str:
        return self._data.get("vps_name", "VPS")

    @property
    def mt5(self) -> dict:
        return self._merged_section("mt5")

    @property
    def mexc(self) -> dict:
        return dict(self.exchange)

    @property
    def exchange(self) -> dict:
        exchange = self._merged_section("exchange", aliases=("mexc",))
        if exchange:
            exchange.setdefault("name", exchange.get("name", "mexc"))
            return exchange

        return {"name": "mexc"}

    @property
    def arbitrage(self) -> dict:
        return self._merged_section("arbitrage")

    @property
    def brain(self) -> dict:
        return self._merged_section("brain")

    @property
    def safety(self) -> dict:
        return self._merged_section("safety")

    @property
    def schedule(self) -> dict:
        return self._merged_section("schedule")

    @property
    def reconciler(self) -> dict:
        return self._merged_section("reconciler")

    @property
    def watchdog(self) -> dict:
        return self._merged_section("watchdog")

    @property
    def shutdown(self) -> dict:
        return self._merged_section("shutdown")

    @property
    def telegram(self) -> dict:
        return self._merged_section("telegram")

    @property
    def database(self) -> dict:
        database = self._merged_section("database")
        if database:
            return database
        return {"path": "data/arb.db"}

    @property
    def dev_entry(self) -> float:
        return self.arbitrage.get("deviation_entry", 0.15)

    @property
    def dev_close(self) -> float:
        return self.arbitrage.get("deviation_close", 0.05)

    @property
    def stable_time_ms(self) -> int:
        return self.arbitrage.get("stable_time_ms", 300)

    @property
    def max_orders(self) -> int:
        return self.arbitrage.get("max_orders", 1)

    @property
    def cooldown_entry(self) -> int:
        return self.arbitrage.get("cooldown_entry_sec", 60)

    @property
    def cooldown_close(self) -> int:
        return self.arbitrage.get("cooldown_close_sec", 3)

    @property
    def hold_time(self) -> int:
        return self.arbitrage.get("hold_time_sec", 240)

    @property
    def pivot_ema_minutes(self) -> float:
        return float(self.arbitrage.get("pivot_ema_minutes", 15.0))

    @property
    def pivot_ema_sec(self) -> float:
        return max(self.pivot_ema_minutes * 60.0, 0.001)

    @property
    def warmup_pivot(self) -> float:
        return float(self.arbitrage.get("warmup_pivot", -20.0))

    @property
    def allow_entry_during_warmup(self) -> bool:
        return bool(self.arbitrage.get("allow_entry_during_warmup", False))

    @property
    def warmup_sec(self) -> int:
        return self.brain.get("warmup_sec", 300)

    @property
    def warmup_min_ticks(self) -> int:
        return self.brain.get("warmup_min_ticks", 500)

    @property
    def max_tick_delay(self) -> float:
        return self.safety.get("max_tick_delay_sec", 10.0)

    @property
    def alert_equity_mt5(self) -> float:
        return float(self.safety.get("alert_equity_mt5", 0.0))

    @property
    def orphan_max_count(self) -> int:
        return int(self.safety.get("orphan_max_count", 0))

    @property
    def orphan_cooldown_sec(self) -> int:
        return int(self.safety.get("orphan_cooldown_sec", 0))

    @property
    def auth_check_interval_sec(self) -> int:
        return self.safety.get("auth_check_interval_sec", 300)

    @property
    def mt5_volume(self) -> float:
        return self.mt5.get("volume", 0.01)

    @property
    def mt5_position_check_interval_ms(self) -> int:
        return self.mt5.get("position_check_interval_ms", 30)

    @property
    def mexc_volume(self) -> int:
        return self.exchange.get("volume", self.mexc.get("volume", 1000))

    @property
    def mexc_leverage(self) -> int:
        return self.exchange.get("leverage", self.mexc.get("leverage", 10))

    @property
    def mexc_symbol(self) -> str:
        return self.exchange.get("symbol", self.mexc.get("symbol", "XAUT_USDT"))

    @property
    def exchange_name(self) -> str:
        return self.exchange.get("name", "mexc")

    @property
    def mt5_symbol(self) -> str:
        return self.mt5.get("symbol", "XAUUSD")

    @property
    def mexc_web_token(self) -> str:
        return self.exchange.get("web_token", self.mexc.get("web_token", ""))

    @property
    def database_path(self) -> str:
        return self.database.get("path", "data/arb.db")

    @property
    def brain_heartbeat_interval_sec(self) -> int:
        return self.brain.get("heartbeat_interval_sec", 10)

    @property
    def brain_log_interval_sec(self) -> int:
        return self.brain.get("log_interval_sec", 5)

    @property
    def on_shutdown_positions(self) -> str:
        return self.shutdown.get("on_shutdown_positions", "leave")

    @property
    def shutdown_grace_period_sec(self) -> int:
        return self.shutdown.get("grace_period_sec", 30)

    @property
    def reconciler_interval_sec(self) -> int:
        return int(self.reconciler.get("interval_sec", 30))

    @property
    def reconciler_auto_heal(self) -> bool:
        return bool(self.reconciler.get("auto_heal", True))

    @property
    def reconciler_ghost_action(self) -> str:
        return str(self.reconciler.get("ghost_action", "alert")).lower()

    @property
    def reconciler_alert_telegram(self) -> bool:
        return bool(self.reconciler.get("alert_telegram", True))

    @property
    def reconciler_stale_entry_sec(self) -> int:
        thresholds = self.reconciler.get("thresholds", {})
        return int(thresholds.get("stale_entry_sec", 60))

    @property
    def reconciler_stale_close_sec(self) -> int:
        thresholds = self.reconciler.get("thresholds", {})
        return int(thresholds.get("stale_close_sec", 120))

    @property
    def reconciler_stale_healing_sec(self) -> int:
        thresholds = self.reconciler.get("thresholds", {})
        return int(thresholds.get("stale_healing_sec", 300))

    @property
    def watchdog_check_interval_sec(self) -> int:
        return int(self.watchdog.get("check_interval_sec", 5))

    @property
    def watchdog_max_restarts(self) -> int:
        return int(self.watchdog.get("max_restarts", 3))

    @property
    def watchdog_restart_window_sec(self) -> int:
        return int(self.watchdog.get("restart_window_sec", 300))

    def __repr__(self) -> str:
        return f"Config({self.config_path})"

