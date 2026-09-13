PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    role_type TEXT NOT NULL CHECK (role_type IN ('booster', 'staff', 'admin')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    booster_level INTEGER NOT NULL DEFAULT 0 CHECK (booster_level >= 0),
    balance_cents INTEGER NOT NULL DEFAULT 0,
    total_earned_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_earned_cents >= 0),
    total_paid_out_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_paid_out_cents >= 0),
    completed_orders_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_orders_count >= 0),
    boosted_since TEXT,
    verified_booster INTEGER NOT NULL DEFAULT 0 CHECK (verified_booster IN (0, 1)),
    rules_confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL DEFAULT '',
    referral_code TEXT,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (team_id, user_id),
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS referral_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_code_id INTEGER NOT NULL,
    referrer_user_id INTEGER NOT NULL,
    referred_discord_id INTEGER,
    referred_name TEXT,
    total_bonus_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_bonus_cents >= 0),
    successful_orders_count INTEGER NOT NULL DEFAULT 0 CHECK (successful_orders_count >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (referral_code_id) REFERENCES referral_codes(id) ON DELETE RESTRICT,
    FOREIGN KEY (referrer_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    customer_discord_id INTEGER,
    customer_name TEXT,
    note TEXT,
    region TEXT NOT NULL,
    queue_type TEXT NOT NULL,
    current_rank TEXT NOT NULL,
    target_rank TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'open',
            'claimed',
            'in_progress',
            'completed',
            'cancelled',
            'disputed'
        )
    ),
    order_price_cents INTEGER NOT NULL CHECK (order_price_cents >= 0),
    booster_payout_cents INTEGER NOT NULL CHECK (booster_payout_cents >= 0),
    referral_bonus_cents INTEGER NOT NULL DEFAULT 0 CHECK (referral_bonus_cents >= 0),
    created_by_user_id INTEGER NOT NULL,
    assigned_booster_user_id INTEGER,
    referral_id INTEGER,
    posted_channel_id INTEGER,
    posted_message_id INTEGER,
    account_username TEXT,
    account_password TEXT,
    claim_deadline_hours INTEGER,
    deadline_at TEXT,
    deadline_reminder_sent_at TEXT,
    claimed_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT,
    FOREIGN KEY (assigned_booster_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (referral_id) REFERENCES referrals(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    referral_id INTEGER,
    requested_by_user_id INTEGER,
    approved_by_user_id INTEGER,
    type TEXT NOT NULL CHECK (
        type IN (
            'order_payout',
            'referral_bonus',
            'withdrawal_request',
            'manual_adjustment'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'paid', 'cancelled')
    ),
    amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0),
    note TEXT,
    payout_ticket_channel_id INTEGER,
    processed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    FOREIGN KEY (referral_id) REFERENCES referrals(id) ON DELETE SET NULL,
    FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (approved_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_user_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    form_payload_json TEXT NOT NULL,
    reviewed_by_user_id INTEGER,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (applicant_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_type TEXT NOT NULL CHECK (ticket_type IN ('support', 'payout', 'application', 'order_completion')),
    opener_user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    related_transaction_id INTEGER,
    related_application_id INTEGER,
    related_order_number TEXT,
    closed_by_user_id INTEGER,
    closed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opener_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (related_transaction_id) REFERENCES transactions(id) ON DELETE SET NULL,
    FOREIGN KEY (related_application_id) REFERENCES applications(id) ON DELETE SET NULL,
    FOREIGN KEY (closed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_assigned_booster ON orders(assigned_booster_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer_user_id ON referrals(referrer_user_id);
