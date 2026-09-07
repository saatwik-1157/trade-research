"""Import every model so Base.metadata is complete.

Alembic autogenerate and the test suite's create_all both rely on this
module having been imported. `EXPECTED_TABLES` is the contract L05 promised;
a test fails if a table is missing.
"""

from __future__ import annotations

from app.auth.models import AuthSession, PasswordResetToken, User
from app.models.accounts import (
    ROLE_SEED,
    BrokerAccount,
    MT5Connection,
    PaperAccount,
    RoleRow,
    seed_roles,
)
from app.models.ai import AIModel, ModelPrediction, ModelVersion, TrainingRun
from app.models.ai_integration import AiDecisionRecord, AiStrategyConfiguration
from app.models.ai_registry import ModelDeployment, ModelLifecycleEvent
from app.models.bots import Bot, BotEvent, BotRun
from app.models.datasets import DatasetCheck, DatasetRecord, FeatureSet, LabelSet
from app.models.execution import Execution, Order, OrderEvent, Position, PositionEvent, Trade
from app.models.journal import JournalEntry, PortfolioSnapshot, TradeTag
from app.models.market import MarketBar, Symbol, SymbolMapping
from app.models.monitoring import ModelAlert, ModelMonitoringSnapshot
from app.models.ops import (
    AuditLog,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    SystemEvent,
)
from app.models.research import Backtest, BacktestTrade, PaperOrder, PaperPosition, ReplaySession
from app.models.review import TradeReviewRow
from app.models.risk import CapitalReservation, RiskEvent, RiskRule
from app.models.signals import Signal, WebhookEvent
from app.models.strategies import Strategy, StrategyParameter, StrategyVersion
from app.models.validation import ValidationRun

# The brief's "sessions" table is auth_sessions (L04), kept under its name
# rather than renamed: a rename is a destructive change with no benefit.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "roles",
        "auth_sessions",
        "password_reset_tokens",
        # L54/L55. Risk budget an approval has claimed and no fill has consumed
        # yet. It is a TABLE rather than a dict because the dict was empty
        # after a restart, and an approval that reserved and had not filled
        # released nothing -- the next process believed the whole budget free.
        "capital_reservations",
        "broker_accounts",
        "mt5_connections",
        "symbols",
        "symbol_mappings",
        "market_bars",
        "strategies",
        "strategy_versions",
        "strategy_parameters",
        "signals",
        "webhook_events",
        "orders",
        "order_events",
        "executions",
        "positions",
        "position_events",
        "trades",
        "backtests",
        "backtest_trades",
        "replay_sessions",
        "paper_accounts",
        "paper_orders",
        "paper_positions",
        "risk_rules",
        "risk_events",
        "bots",
        "bot_runs",
        "bot_events",
        "portfolio_snapshots",
        "trade_reviews",
        "journal_entries",
        "trade_tags",
        "models",
        "model_versions",
        "training_runs",
        "model_predictions",
        "feature_sets",
        "label_sets",
        "datasets",
        "dataset_checks",
        "validation_runs",
        "ai_strategy_configs",
        "ai_decisions",
        "model_deployments",
        "model_lifecycle_events",
        "model_monitoring_snapshots",
        "model_alerts",
        "notifications",
        "notification_deliveries",
        "notification_preferences",
        "audit_logs",
        "system_events",
    }
)

__all__ = [
    "EXPECTED_TABLES",
    "ROLE_SEED",
    "AIModel",
    "AiDecisionRecord",
    "AiStrategyConfiguration",
    "AuditLog",
    "AuthSession",
    "PasswordResetToken",
    "Backtest",
    "BacktestTrade",
    "Bot",
    "BotEvent",
    "BotRun",
    "BrokerAccount",
    "DatasetCheck",
    "DatasetRecord",
    "Execution",
    "FeatureSet",
    "JournalEntry",
    "LabelSet",
    "MT5Connection",
    "ModelAlert",
    "TradeReviewRow",
    "ModelDeployment",
    "ModelLifecycleEvent",
    "ModelMonitoringSnapshot",
    "ModelPrediction",
    "ModelVersion",
    "Notification",
    "NotificationDelivery",
    "NotificationPreference",
    "Order",
    "OrderEvent",
    "PaperAccount",
    "PaperOrder",
    "PaperPosition",
    "PortfolioSnapshot",
    "Position",
    "PositionEvent",
    "ReplaySession",
    "CapitalReservation",
    "RiskEvent",
    "RiskRule",
    "RoleRow",
    "Signal",
    "Strategy",
    "StrategyParameter",
    "StrategyVersion",
    "MarketBar",
    "Symbol",
    "SymbolMapping",
    "SystemEvent",
    "Trade",
    "TradeTag",
    "TrainingRun",
    "User",
    "ValidationRun",
    "WebhookEvent",
    "seed_roles",
]
