-- Wheel Copilot — Full Database Schema
-- All tables use DECIMAL for money columns, NEVER float.

-- ============================================================
-- TRADE TRACKING
-- ============================================================

CREATE TABLE recommendations (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    strike DECIMAL(10,2),
    expiration DATE,
    premium_target DECIMAL(10,4),
    contracts INT,
    conviction VARCHAR(10),
    strategy VARCHAR(30),
    signals TEXT[],
    reasoning TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE executions (
    id SERIAL PRIMARY KEY,
    recommendation_id INT REFERENCES recommendations(id),
    executed BOOLEAN DEFAULT FALSE,
    execution_price DECIMAL(10,4),
    execution_time TIMESTAMP,
    slippage DECIMAL(10,4),
    fees DECIMAL(10,4),
    account_id VARCHAR(50),
    notes TEXT
);

CREATE TABLE daily_snapshots (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    net_liquidation DECIMAL(12,2),
    daily_theta DECIMAL(10,2),
    portfolio_delta DECIMAL(10,2),
    portfolio_beta_delta DECIMAL(10,2),
    num_positions INT,
    num_signals_fired INT,
    num_trades_executed INT,
    adbe_concentration DECIMAL(5,4),
    capital_efficiency DECIMAL(10,6),
    idle_capital_pct DECIMAL(5,4),
    margin_utilization DECIMAL(5,4),
    regime VARCHAR(20),
    vix_close DECIMAL(5,2),
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- ONBOARDING
-- ============================================================

CREATE TABLE position_classifications (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    engine VARCHAR(10) NOT NULL,
    conviction VARCHAR(10),
    classification_date DATE NOT NULL,
    shares INT,
    cost_basis DECIMAL(12,2),
    classified_by VARCHAR(10) DEFAULT 'user',
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE transition_actions (
    id SERIAL PRIMARY KEY,
    urgency VARCHAR(20) NOT NULL,
    action VARCHAR(20) NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    description TEXT,
    tax_impact DECIMAL(12,2),
    status VARCHAR(20) DEFAULT 'pending',
    completed_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- WASH SALE TRACKING
-- ============================================================

CREATE TABLE wash_sale_tracker (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    loss_date DATE NOT NULL,
    loss_amount DECIMAL(12,2),
    wash_sale_window_end DATE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- PAPER TRADING
-- ============================================================

CREATE TABLE paper_trades (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL,
    trade_type VARCHAR(20) NOT NULL,
    strike DECIMAL(10,2),
    expiration DATE,
    contracts INT,
    conviction VARCHAR(10),
    strategy VARCHAR(30),
    signals TEXT[],

    -- Entry
    entry_price DECIMAL(10,4),
    entry_time TIMESTAMP,
    entry_underlying DECIMAL(10,2),
    entry_iv_rank DECIMAL(5,1),
    capital_at_risk DECIMAL(12,2),

    -- Exit
    exit_price DECIMAL(10,4),
    exit_time TIMESTAMP,
    exit_underlying DECIMAL(10,2),
    exit_reason VARCHAR(50),

    -- P&L
    pnl DECIMAL(12,2),
    pnl_pct DECIMAL(8,4),

    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE paper_daily_snapshots (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    capital DECIMAL(12,2),
    buying_power DECIMAL(12,2),
    open_positions INT,
    daily_pnl DECIMAL(12,2),
    cumulative_pnl DECIMAL(12,2),
    max_drawdown DECIMAL(8,4),
    win_rate DECIMAL(5,4),
    trades_to_date INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- LEARNING LOOP
-- ============================================================

CREATE TABLE parameter_adjustments (
    id SERIAL PRIMARY KEY,
    adjustment_date DATE NOT NULL,
    review_type VARCHAR(20) NOT NULL,
    param_name VARCHAR(100) NOT NULL,
    old_value DECIMAL(10,6),
    new_value DECIMAL(10,6),
    reason TEXT,
    approved BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE signal_performance_history (
    id SERIAL PRIMARY KEY,
    week_ending DATE NOT NULL,
    signal_type VARCHAR(50) NOT NULL,
    trade_count INT,
    win_rate DECIMAL(5,4),
    avg_return DECIMAL(8,4),
    sharpe_ratio DECIMAL(6,3),
    optimal_threshold DECIMAL(10,4),
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- TAX TRACKING
-- ============================================================

CREATE TABLE tax_events (
    id SERIAL PRIMARY KEY,
    trade_id INT REFERENCES paper_trades(id),
    event_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    event_type VARCHAR(30) NOT NULL,
    gross_pnl DECIMAL(12,2),
    is_short_term BOOLEAN,
    holding_period_days INT,
    tax_rate DECIMAL(5,4),
    estimated_tax DECIMAL(12,2),
    loss_offset_applied DECIMAL(12,2),
    wash_sale_triggered BOOLEAN DEFAULT FALSE,
    wash_sale_disallowed_loss DECIMAL(12,2),
    account_type VARCHAR(20),
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE quarterly_tax_estimates (
    id SERIAL PRIMARY KEY,
    quarter VARCHAR(10) NOT NULL,
    estimated_stcg DECIMAL(12,2),
    estimated_ltcg DECIMAL(12,2),
    estimated_losses DECIMAL(12,2),
    estimated_tax_owed DECIMAL(12,2),
    payment_due_date DATE,
    payment_made BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- OPERATIONAL
-- ============================================================

CREATE TABLE overnight_assignments (
    id SERIAL PRIMARY KEY,
    detected_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    shares INT NOT NULL,
    assignment_strike DECIMAL(10,2),
    premium_received DECIMAL(10,4),
    cost_basis DECIMAL(10,2),
    account_id VARCHAR(50),
    engine VARCHAR(10),
    covered_call_sold BOOLEAN DEFAULT FALSE,
    covered_call_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE dividend_receipts (
    id SERIAL PRIMARY KEY,
    receipt_date DATE NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    amount DECIMAL(10,2),
    shares INT,
    engine VARCHAR(10),
    routing_action VARCHAR(30),
    reinvested BOOLEAN DEFAULT FALSE,
    reinvest_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE vesting_events (
    id SERIAL PRIMARY KEY,
    vest_date DATE NOT NULL,
    vest_type VARCHAR(10) NOT NULL,
    estimated_shares INT,
    estimated_value DECIMAL(12,2),
    actual_shares INT,
    actual_value DECIMAL(12,2),
    sold BOOLEAN DEFAULT FALSE,
    sold_date DATE,
    sold_price DECIMAL(10,2),
    redeployment_plan TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE system_health (
    id SERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    broker_api_status VARCHAR(10),
    database_status VARCHAR(10),
    last_successful_analysis TIMESTAMP,
    discrepancies_found INT DEFAULT 0,
    discrepancies_resolved INT DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_recommendations_date ON recommendations(date);
CREATE INDEX idx_recommendations_symbol ON recommendations(symbol);
CREATE INDEX idx_paper_trades_symbol ON paper_trades(symbol);
CREATE INDEX idx_paper_trades_entry_time ON paper_trades(entry_time);
CREATE INDEX idx_tax_events_date ON tax_events(event_date);
CREATE INDEX idx_tax_events_symbol ON tax_events(symbol);
CREATE INDEX idx_wash_sale_symbol ON wash_sale_tracker(symbol);
CREATE INDEX idx_wash_sale_active ON wash_sale_tracker(is_active);
CREATE INDEX idx_signal_perf_week ON signal_performance_history(week_ending);
CREATE INDEX idx_daily_snapshots_date ON daily_snapshots(date);
