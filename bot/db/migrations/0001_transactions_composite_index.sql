CREATE INDEX IF NOT EXISTS idx_transactions_type_status
ON transactions(type, status);
