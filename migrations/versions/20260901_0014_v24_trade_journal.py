"""Add the isolated v2.4 auditable trade journal.

Revision ID: 20260901_0014
Revises: 20260827_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0014"
down_revision = "20260827_0013"
branch_labels = None
depends_on = None

IMMUTABLE_TABLES = ("idea_journals", "decision_snapshots_v24", "trade_event_journal")


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION v24_reject_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only and cannot be %', TG_TABLE_NAME, TG_OP;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in IMMUTABLE_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION v24_reject_mutation()
                """
            )
    elif dialect == "sqlite":
        for table in IMMUTABLE_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is immutable');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute("DROP FUNCTION IF EXISTS v24_reject_mutation()")
    elif dialect == "sqlite":
        for table in IMMUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def upgrade() -> None:
    op.create_table(
        "trade_id_sequences",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("trade_date", "ticker", "direction"),
    )

    op.create_table(
        "idea_journals",
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("signal_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("setup", sa.String(length=40), nullable=True),
        sa.Column("market_regime", sa.String(length=32), nullable=True),
        sa.Column("market_bias", sa.String(length=24), nullable=True),
        sa.Column("data_confidence", sa.String(length=16), nullable=True),
        sa.Column("data_sla_status", sa.String(length=24), nullable=True),
        sa.Column("data_sla_result", sa.String(length=24), nullable=True),
        sa.Column("price_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_delay_seconds", sa.Float(), nullable=True),
        sa.Column("source_set", sa.Text(), nullable=True),
        sa.Column("fundamental_context", sa.Text(), nullable=True),
        sa.Column("news_context", sa.Text(), nullable=True),
        sa.Column("catalyst", sa.Text(), nullable=True),
        sa.Column("setup_quality", sa.Float(), nullable=True),
        sa.Column("execution_quality", sa.Float(), nullable=True),
        sa.Column("optimal_entry", sa.Float(), nullable=True),
        sa.Column("acceptable_entry", sa.Float(), nullable=True),
        sa.Column("no_chase_level", sa.Float(), nullable=True),
        sa.Column("initial_stop", sa.Float(), nullable=True),
        sa.Column("tp1", sa.Float(), nullable=True),
        sa.Column("tp2", sa.Float(), nullable=True),
        sa.Column("runner_target", sa.Float(), nullable=True),
        sa.Column("path_to_tp_status", sa.String(length=32), nullable=True),
        sa.Column("gross_rr", sa.Float(), nullable=True),
        sa.Column("expected_costs", sa.Float(), nullable=True),
        sa.Column("liquidity_cap", sa.Float(), nullable=True),
        sa.Column("normal_exit_cap", sa.Float(), nullable=True),
        sa.Column("fast_exit_cap", sa.Float(), nullable=True),
        sa.Column("stress_exit_cap", sa.Float(), nullable=True),
        sa.Column("risk_cap", sa.Float(), nullable=True),
        sa.Column("max_safe_position", sa.Float(), nullable=True),
        sa.Column("recommended_position", sa.Float(), nullable=True),
        sa.Column("risk_to_stop_rub", sa.Float(), nullable=True),
        sa.Column("risk_to_stop_pct_capital", sa.Float(), nullable=True),
        sa.Column("probability_status", sa.String(length=32), nullable=True),
        sa.Column("stated_probability", sa.Float(), nullable=True),
        sa.Column("calibration_group", sa.String(length=64), nullable=True),
        sa.Column("statistical_admission_status", sa.String(length=24), nullable=True),
        sa.Column("opportunity_cost", sa.Float(), nullable=True),
        sa.Column("microstructure_status", sa.String(length=24), nullable=True),
        sa.Column("risk_budget_status", sa.String(length=24), nullable=True),
        sa.Column("journal_status", sa.String(length=24), nullable=True),
        sa.Column("audit_status", sa.String(length=16), nullable=True),
        sa.Column("final_classification", sa.String(length=48), nullable=True),
        sa.Column("final_decision", sa.String(length=24), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("reason_for_trade", sa.Text(), nullable=True),
        sa.Column("adversarial_result", sa.String(length=16), nullable=True),
        _created_at(),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_idea_journal_direction"),
        sa.PrimaryKeyConstraint("trade_id"),
    )
    op.create_index("ix_idea_journals_signal", "idea_journals", ["signal_datetime"])
    op.create_index("ix_idea_journals_ticker", "idea_journals", ["ticker", "signal_datetime"])
    op.create_index(
        "ix_idea_journals_strategy",
        "idea_journals",
        ["strategy_version", "signal_datetime"],
    )

    op.create_table(
        "decision_snapshots_v24",
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("signal_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("price_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_delay_seconds", sa.Float(), nullable=True),
        sa.Column("data_sla", sa.Text(), nullable=True),
        sa.Column("sources", sa.Text(), nullable=True),
        sa.Column("market_regime", sa.String(length=32), nullable=True),
        sa.Column("market_bias", sa.String(length=24), nullable=True),
        sa.Column("setup", sa.String(length=40), nullable=True),
        sa.Column("setup_quality", sa.Float(), nullable=True),
        sa.Column("execution_quality", sa.Float(), nullable=True),
        sa.Column("entry", sa.Text(), nullable=True),
        sa.Column("stop", sa.Float(), nullable=True),
        sa.Column("tp1", sa.Float(), nullable=True),
        sa.Column("tp2", sa.Float(), nullable=True),
        sa.Column("liquidity_inputs", sa.Text(), nullable=True),
        sa.Column("risk_inputs", sa.Text(), nullable=True),
        sa.Column("news_context", sa.Text(), nullable=True),
        sa.Column("event_context", sa.Text(), nullable=True),
        sa.Column("calibration_group", sa.String(length=64), nullable=True),
        sa.Column("final_decision", sa.String(length=24), nullable=True),
        sa.Column("gate_results", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "evidence_status", sa.String(length=40), nullable=False, server_default="AVAILABLE"
        ),
        _created_at(),
        sa.ForeignKeyConstraint(["trade_id"], ["idea_journals.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("trade_id"),
    )
    op.create_index(
        "ix_decision_snapshots_v24_signal_datetime",
        "decision_snapshots_v24",
        ["signal_datetime"],
    )
    op.create_index("ix_decision_snapshots_v24_ticker", "decision_snapshots_v24", ["ticker"])

    op.create_table(
        "model_trade_journals",
        sa.Column("model_trade_id", sa.String(length=72), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("sample_type", sa.String(length=16), nullable=False),
        sa.Column("calibration_group", sa.String(length=64), nullable=True),
        sa.Column("calibration_eligible", sa.Boolean(), nullable=False),
        sa.Column("model_entry_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_entry", sa.Float(), nullable=True),
        sa.Column("model_order_type", sa.String(length=16), nullable=True),
        sa.Column("model_fill_status", sa.String(length=16), nullable=False),
        sa.Column("initial_stop", sa.Float(), nullable=True),
        sa.Column("tp1", sa.Float(), nullable=True),
        sa.Column("tp2", sa.Float(), nullable=True),
        sa.Column("model_position_rub", sa.Float(), nullable=True),
        sa.Column("entry_costs", sa.Float(), nullable=True),
        sa.Column("exit_costs", sa.Float(), nullable=True),
        sa.Column("final_exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_exit", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("gross_pl_rub", sa.Float(), nullable=True),
        sa.Column("net_pl_rub", sa.Float(), nullable=True),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("initial_risk_rub", sa.Float(), nullable=True),
        sa.Column("result_r", sa.Float(), nullable=True),
        sa.Column("win_1_0", sa.Integer(), nullable=True),
        sa.Column("tp_before_sl_1_0", sa.Integer(), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),
        sa.Column("mae_pct", sa.Float(), nullable=True),
        sa.Column("capture_ratio", sa.Float(), nullable=True),
        sa.Column("holding_hours", sa.Float(), nullable=True),
        sa.Column("stated_probability", sa.Float(), nullable=True),
        sa.Column("brier_score", sa.Float(), nullable=True),
        sa.Column("setup_quality_at_entry", sa.Float(), nullable=True),
        sa.Column("execution_quality_at_entry", sa.Float(), nullable=True),
        sa.Column("data_sla_at_entry", sa.String(length=24), nullable=True),
        sa.Column("ambiguous_execution", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("gap_slippage", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["trade_id"], ["idea_journals.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("model_trade_id"),
        sa.UniqueConstraint("trade_id", name="uq_model_trade_journal_trade"),
    )
    op.create_index("ix_model_trade_journals_trade_id", "model_trade_journals", ["trade_id"])
    op.create_index(
        "ix_model_trade_sample",
        "model_trade_journals",
        ["sample_type", "model_entry_time"],
    )
    op.create_index(
        "ix_model_trade_strategy",
        "model_trade_journals",
        ["strategy_version", "model_entry_time"],
    )

    op.create_table(
        "actual_trade_journals",
        sa.Column("actual_trade_id", sa.String(length=72), nullable=False),
        sa.Column("model_trade_id", sa.String(length=72), nullable=True),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("confirmation_key", sa.String(length=128), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_entry", sa.Float(), nullable=False),
        sa.Column("actual_position_rub", sa.Float(), nullable=True),
        sa.Column("actual_position_shares", sa.Float(), nullable=True),
        sa.Column("model_entry", sa.Float(), nullable=True),
        sa.Column("entry_slippage_bps", sa.Float(), nullable=True),
        sa.Column("initial_stop", sa.Float(), nullable=True),
        sa.Column("tp1", sa.Float(), nullable=True),
        sa.Column("tp2", sa.Float(), nullable=True),
        sa.Column("actual_entry_costs", sa.Float(), nullable=True),
        sa.Column("actual_exit_costs", sa.Float(), nullable=True),
        sa.Column("final_exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_exit", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("gross_pl_rub", sa.Float(), nullable=True),
        sa.Column("net_pl_rub", sa.Float(), nullable=True),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("initial_risk_rub", sa.Float(), nullable=True),
        sa.Column("result_r", sa.Float(), nullable=True),
        sa.Column("win_1_0", sa.Integer(), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),
        sa.Column("mae_pct", sa.Float(), nullable=True),
        sa.Column("capture_ratio", sa.Float(), nullable=True),
        sa.Column("holding_hours", sa.Float(), nullable=True),
        sa.Column("execution_quality_actual", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["model_trade_id"],
            ["model_trade_journals.model_trade_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["idea_journals.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("actual_trade_id"),
        sa.UniqueConstraint("confirmation_key", name="uq_actual_trade_confirmation"),
        sa.UniqueConstraint("trade_id", name="uq_actual_trade_journal_trade"),
    )
    op.create_index("ix_actual_trade_journals_trade_id", "actual_trade_journals", ["trade_id"])
    op.create_index(
        "ix_actual_trade_strategy",
        "actual_trade_journals",
        ["strategy_version", "actual_entry_time"],
    )

    op.create_table(
        "trade_event_journal",
        sa.Column("event_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("model_trade_id", sa.String(length=72), nullable=True),
        sa.Column("actual_trade_id", sa.String(length=72), nullable=True),
        sa.Column("event_datetime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("stop_before", sa.Float(), nullable=True),
        sa.Column("stop_after", sa.Float(), nullable=True),
        sa.Column("tp_before", sa.Float(), nullable=True),
        sa.Column("tp_after", sa.Float(), nullable=True),
        sa.Column("position_before", sa.Float(), nullable=True),
        sa.Column("position_after", sa.Float(), nullable=True),
        sa.Column("data_sla", sa.String(length=24), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_or_broker_note", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["actual_trade_id"],
            ["actual_trade_journals.actual_trade_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_trade_id"],
            ["model_trade_journals.model_trade_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["idea_journals.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_trade_event_trade", "trade_event_journal", ["trade_id", "event_datetime"])
    op.create_index(
        "ix_trade_event_model",
        "trade_event_journal",
        ["model_trade_id", "event_datetime"],
    )
    op.create_index(
        "ix_trade_event_actual",
        "trade_event_journal",
        ["actual_trade_id", "event_datetime"],
    )
    op.create_index("ix_trade_event_journal_trade_id", "trade_event_journal", ["trade_id"])

    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_table("trade_event_journal")
    op.drop_table("actual_trade_journals")
    op.drop_table("model_trade_journals")
    op.drop_table("decision_snapshots_v24")
    op.drop_table("idea_journals")
    op.drop_table("trade_id_sequences")
