-- HTTP 控制面 side-meta schema（T-04·ADR-0022 / ADR-0024）。
--
-- 三表为 control-plane 的 side metadata（state 仍由 AsyncPostgresSaver checkpointer 承载，
-- 本处**不含** get_state/save_state）。``PostgresSessionCache.setup()`` 幂等执行本脚本
-- （CREATE TABLE IF NOT EXISTS）。
--
-- 孤儿锁 / pause_meta 后台 sweep 属 T-08；本切片仅惰性清理（请求路径命中过期即失效）。

-- pause_meta：HITL 暂停点元数据。fresh run 到达 hitl interrupt 时写；resume 续跑时读；
-- 终态 / 过期时删。pause_time 超 30min（PAUSE_TTL_SECONDS）→ PAUSE_EXPIRED。
CREATE TABLE IF NOT EXISTS pause_meta (
    session_id  TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    pause_time  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session_owner：会话所有权登记。session_id 首见时登记 + 绑定 X-User-Id；
-- 已登记不匹配 → 403 FORBIDDEN。last_seen 近 30min 计活跃会话数 → SESSION_LIMIT。
CREATE TABLE IF NOT EXISTS session_owner (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session_locks：执行锁行（ADR-0022：跨请求持有、HITL 暂停期留存，不用 pg_advisory_lock）。
-- fresh query：INSERT ... ON CONFLICT DO NOTHING；冲突且未过期 → LOCK_EXIST；过期则接管。
-- HITL 暂停期不释放（行留存、续跑复用、不再 INSERT 故不误触 LOCK_EXIST）；终态 / abort 删行。
-- last_heartbeat + ttl_seconds 兜底孤儿 run（T-08 后台 sweep）。
CREATE TABLE IF NOT EXISTS session_locks (
    session_id     TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    acquired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_seconds    INT NOT NULL DEFAULT 900
);
