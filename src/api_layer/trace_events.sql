-- trace_events 持久日志表（T-05·ADR-0023·PRD §4.2.2）。
--
-- 翻译层（astream_events 驱动）每事件写一行、非阻塞、mint event_seq。
-- 本表是显示层的**durable 回放源**——WS-sender（T-06）只读尾随；WS 断开不中止 run
-- （ADR-0023 不变量）。``PostgresTraceEventStore.setup()`` 幂等执行本脚本
-- （CREATE TABLE IF NOT EXISTS）。
--
-- 与 schema.sql 的三张 side-meta 表（pause_meta / session_owner / session_locks）解耦：
-- 本表属显示层（事件日志），非控制面 side metadata。state 仍由 AsyncPostgresSaver 承载。
CREATE TABLE IF NOT EXISTS trace_events (
    session_id  TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    event_seq   INT  NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trace_id, event_seq)
);

-- 回放按序：单 trace 内 event_seq 单调；按 (session_id, trace_id, event_seq) 索引覆盖
-- 「查某 session 的某 trace 全量事件」与「续跑 max(event_seq) 派生」两类查询。
CREATE INDEX IF NOT EXISTS trace_events_session_trace_seq_idx
    ON trace_events (session_id, trace_id, event_seq);
