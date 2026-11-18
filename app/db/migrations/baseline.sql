-- rmp_db's schema as the nightly backup 20261112T031513Z holds it (specs/WP-26.md WP26-02)
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--




--
-- Name: artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.artifacts (
    id character varying NOT NULL,
    process_run_id character varying,
    kind character varying,
    uri character varying,
    checksum character varying,
    mime_type character varying,
    created_at timestamp without time zone,
    filename character varying,
    size_bytes integer,
    storage_key character varying
);


--
-- Name: dm_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_chunks (
    id character varying NOT NULL,
    document_id character varying NOT NULL,
    section_id character varying,
    ordinal integer NOT NULL,
    text text NOT NULL,
    context_header text,
    char_count integer,
    task_id character varying,
    session_key character varying,
    process_run_id character varying,
    message_id character varying,
    role character varying,
    source_at timestamp without time zone,
    meta json,
    created_at timestamp without time zone,
    valid_to timestamp without time zone
);


--
-- Name: dm_context_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_context_reports (
    id character varying NOT NULL,
    task_id character varying NOT NULL,
    trigger character varying,
    status character varying,
    queries json,
    candidate_ids json,
    report json,
    brief text,
    latency_ms integer,
    usage json,
    consumed_by character varying,
    novelty json,
    created_at timestamp without time zone,
    completed_at timestamp without time zone
);


--
-- Name: dm_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_documents (
    id character varying NOT NULL,
    kind character varying NOT NULL,
    source_key character varying NOT NULL,
    title text,
    task_id character varying,
    session_key character varying,
    process_run_id character varying,
    source_ref json,
    content_hash character varying,
    summary text,
    toc json,
    status character varying,
    meta json,
    source_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    enriched_at timestamp without time zone,
    valid_to timestamp without time zone
);


--
-- Name: dm_ingest_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_ingest_queue (
    id character varying NOT NULL,
    kind character varying NOT NULL,
    ref_id character varying NOT NULL,
    priority integer,
    attempts integer,
    last_error text,
    next_attempt_at timestamp without time zone,
    done_at timestamp without time zone,
    created_at timestamp without time zone
);


--
-- Name: dm_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_links (
    id character varying NOT NULL,
    source_type character varying NOT NULL,
    source_id character varying NOT NULL,
    target_type character varying NOT NULL,
    target_id character varying NOT NULL,
    relation character varying NOT NULL,
    meta json,
    created_at timestamp without time zone
);


--
-- Name: dm_sections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_sections (
    id character varying NOT NULL,
    document_id character varying NOT NULL,
    ordinal integer NOT NULL,
    path text,
    title text,
    level integer,
    summary text,
    chunk_count integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.events (
    id character varying NOT NULL,
    correlation_id character varying,
    entity_type character varying,
    entity_id character varying,
    event_type character varying,
    event_payload json,
    occurred_at timestamp without time zone
);


--
-- Name: memory_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_items (
    id character varying NOT NULL,
    scope_type character varying,
    scope_id character varying,
    memory_type character varying,
    content text,
    content_ref json,
    provenance_ref json,
    confidence integer,
    valid_from timestamp without time zone,
    valid_to timestamp without time zone,
    supersedes_memory_id character varying,
    created_at timestamp without time zone
);


--
-- Name: memory_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_links (
    id character varying NOT NULL,
    source_id character varying,
    target_id character varying,
    relation character varying,
    created_at timestamp without time zone
);


--
-- Name: observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.observations (
    id character varying NOT NULL,
    process_run_id character varying,
    source character varying,
    observation_type character varying,
    payload_ref json,
    observed_at timestamp without time zone,
    confidence integer,
    payload_hash character varying
);


--
-- Name: os_process_tracks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.os_process_tracks (
    scanner_id character varying NOT NULL,
    display_name character varying,
    script_basename character varying,
    script_path character varying,
    log_path character varying,
    task_id character varying,
    process_run_id character varying,
    pid integer,
    status character varying,
    last_log_mtime timestamp without time zone,
    last_started_at timestamp without time zone,
    last_seen_at timestamp without time zone,
    run_count integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: process_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.process_runs (
    id character varying NOT NULL,
    task_id character varying,
    process_type character varying,
    plan_version character varying,
    current_state character varying,
    success_criteria json,
    failure_criteria json,
    next_check_at timestamp without time zone,
    started_at timestamp without time zone,
    ended_at timestamp without time zone,
    lease_owner character varying,
    plan_json json,
    parent_process_run_id character varying
);


--
-- Name: side_effect_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.side_effect_receipts (
    id character varying NOT NULL,
    idempotency_key character varying,
    effect_type character varying,
    metadata_ref json,
    created_at timestamp without time zone
);


--
-- Name: steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.steps (
    id character varying NOT NULL,
    process_run_id character varying,
    step_name character varying,
    step_kind character varying,
    status character varying,
    attempt_no integer,
    input_ref json,
    output_ref json,
    started_at timestamp without time zone,
    ended_at timestamp without time zone,
    idempotency_key character varying
);


--
-- Name: task_intake_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_intake_decisions (
    id character varying NOT NULL,
    request_hash character varying,
    decision character varying,
    confidence integer DEFAULT 0,
    rationale text,
    similar_task_ids json,
    llm_raw json,
    policy_overrides json,
    intake_mode character varying DEFAULT 'shadow'::character varying,
    session_key character varying,
    intent_snippet character varying,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: task_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_messages (
    id character varying NOT NULL,
    task_id character varying NOT NULL,
    role character varying DEFAULT 'user'::character varying,
    content text NOT NULL,
    source character varying DEFAULT 'api'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    slack_ts character varying,
    kind character varying,
    session_key character varying,
    meta json
);


--
-- Name: task_registry_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_registry_entries (
    id character varying NOT NULL,
    task_id character varying NOT NULL,
    intent_snippet text,
    outcome_summary text,
    process_type character varying,
    terminal_status character varying,
    task_kind character varying,
    recurrence_key character varying,
    session_key character varying,
    duration_sec integer,
    artifact_refs json,
    vector_point_id character varying,
    indexed_at timestamp without time zone DEFAULT now(),
    task_created_at timestamp without time zone,
    task_ended_at timestamp without time zone
);


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id character varying NOT NULL,
    user_request_id character varying,
    correlation_id character varying,
    requester character varying,
    openclaw_session_key character varying,
    task_type character varying,
    goal character varying,
    status character varying,
    priority integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    idempotency_key character varying,
    next_check_at timestamp without time zone,
    parent_task_id character varying,
    task_kind character varying DEFAULT 'one_shot'::character varying,
    recurrence_key character varying,
    intake_decision_id character varying,
    supplementary_context json
);


--
-- Name: vector_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vector_outbox (
    id character varying NOT NULL,
    kind character varying NOT NULL,
    ref_id character varying NOT NULL,
    attempts integer,
    last_error text,
    next_attempt_at timestamp without time zone,
    done_at timestamp without time zone,
    created_at timestamp without time zone
);


--
-- Name: artifacts artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (id);


--
-- Name: dm_chunks dm_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_chunks
    ADD CONSTRAINT dm_chunks_pkey PRIMARY KEY (id);


--
-- Name: dm_context_reports dm_context_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_context_reports
    ADD CONSTRAINT dm_context_reports_pkey PRIMARY KEY (id);


--
-- Name: dm_documents dm_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_documents
    ADD CONSTRAINT dm_documents_pkey PRIMARY KEY (id);


--
-- Name: dm_documents dm_documents_source_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_documents
    ADD CONSTRAINT dm_documents_source_key_key UNIQUE (source_key);


--
-- Name: dm_ingest_queue dm_ingest_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_ingest_queue
    ADD CONSTRAINT dm_ingest_queue_pkey PRIMARY KEY (id);


--
-- Name: dm_links dm_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_links
    ADD CONSTRAINT dm_links_pkey PRIMARY KEY (id);


--
-- Name: dm_sections dm_sections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_sections
    ADD CONSTRAINT dm_sections_pkey PRIMARY KEY (id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: memory_items memory_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_items
    ADD CONSTRAINT memory_items_pkey PRIMARY KEY (id);


--
-- Name: memory_links memory_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_pkey PRIMARY KEY (id);


--
-- Name: observations observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observations
    ADD CONSTRAINT observations_pkey PRIMARY KEY (id);


--
-- Name: os_process_tracks os_process_tracks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.os_process_tracks
    ADD CONSTRAINT os_process_tracks_pkey PRIMARY KEY (scanner_id);


--
-- Name: process_runs process_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_runs
    ADD CONSTRAINT process_runs_pkey PRIMARY KEY (id);


--
-- Name: side_effect_receipts side_effect_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.side_effect_receipts
    ADD CONSTRAINT side_effect_receipts_pkey PRIMARY KEY (id);


--
-- Name: steps steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.steps
    ADD CONSTRAINT steps_pkey PRIMARY KEY (id);


--
-- Name: task_intake_decisions task_intake_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_intake_decisions
    ADD CONSTRAINT task_intake_decisions_pkey PRIMARY KEY (id);


--
-- Name: task_messages task_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_messages
    ADD CONSTRAINT task_messages_pkey PRIMARY KEY (id);


--
-- Name: task_registry_entries task_registry_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_registry_entries
    ADD CONSTRAINT task_registry_entries_pkey PRIMARY KEY (id);


--
-- Name: task_registry_entries task_registry_entries_task_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_registry_entries
    ADD CONSTRAINT task_registry_entries_task_id_key UNIQUE (task_id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: vector_outbox vector_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vector_outbox
    ADD CONSTRAINT vector_outbox_pkey PRIMARY KEY (id);


--
-- Name: ix_artifacts_checksum; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifacts_checksum ON public.artifacts USING btree (checksum);


--
-- Name: ix_artifacts_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifacts_kind ON public.artifacts USING btree (kind);


--
-- Name: ix_artifacts_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_artifacts_process_run_id ON public.artifacts USING btree (process_run_id);


--
-- Name: ix_dm_chunks_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_document_id ON public.dm_chunks USING btree (document_id);


--
-- Name: ix_dm_chunks_document_ordinal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_document_ordinal ON public.dm_chunks USING btree (document_id, ordinal);


--
-- Name: ix_dm_chunks_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_fts ON public.dm_chunks USING gin (to_tsvector('english'::regconfig, ((COALESCE(context_header, ''::text) || ' '::text) || COALESCE(text, ''::text))));


--
-- Name: ix_dm_chunks_message_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_message_id ON public.dm_chunks USING btree (message_id);


--
-- Name: ix_dm_chunks_section_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_section_id ON public.dm_chunks USING btree (section_id);


--
-- Name: ix_dm_chunks_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_session_key ON public.dm_chunks USING btree (session_key);


--
-- Name: ix_dm_chunks_source_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_source_at ON public.dm_chunks USING btree (source_at);


--
-- Name: ix_dm_chunks_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_chunks_task_id ON public.dm_chunks USING btree (task_id);


--
-- Name: ix_dm_context_reports_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_context_reports_status ON public.dm_context_reports USING btree (status);


--
-- Name: ix_dm_context_reports_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_context_reports_task_id ON public.dm_context_reports USING btree (task_id);


--
-- Name: ix_dm_documents_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_fts ON public.dm_documents USING gin (to_tsvector('english'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || COALESCE(summary, ''::text))));


--
-- Name: ix_dm_documents_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_kind ON public.dm_documents USING btree (kind);


--
-- Name: ix_dm_documents_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_process_run_id ON public.dm_documents USING btree (process_run_id);


--
-- Name: ix_dm_documents_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_session_key ON public.dm_documents USING btree (session_key);


--
-- Name: ix_dm_documents_source_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_source_at ON public.dm_documents USING btree (source_at);


--
-- Name: ix_dm_documents_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_status ON public.dm_documents USING btree (status);


--
-- Name: ix_dm_documents_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_documents_task_id ON public.dm_documents USING btree (task_id);


--
-- Name: ix_dm_ingest_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_ingest_due ON public.dm_ingest_queue USING btree (priority, next_attempt_at) WHERE (done_at IS NULL);


--
-- Name: ix_dm_ingest_queue_done_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_ingest_queue_done_at ON public.dm_ingest_queue USING btree (done_at);


--
-- Name: ix_dm_ingest_queue_ref_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_ingest_queue_ref_id ON public.dm_ingest_queue USING btree (ref_id);


--
-- Name: ix_dm_links_relation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_links_relation ON public.dm_links USING btree (relation);


--
-- Name: ix_dm_links_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_links_source_id ON public.dm_links USING btree (source_id);


--
-- Name: ix_dm_links_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_links_target_id ON public.dm_links USING btree (target_id);


--
-- Name: ix_dm_sections_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_sections_document_id ON public.dm_sections USING btree (document_id);


--
-- Name: ix_dm_sections_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dm_sections_fts ON public.dm_sections USING gin (to_tsvector('english'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || COALESCE(summary, ''::text))));


--
-- Name: ix_events_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_events_correlation_id ON public.events USING btree (correlation_id);


--
-- Name: ix_events_entity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_events_entity_id ON public.events USING btree (entity_id);


--
-- Name: ix_events_type_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_events_type_time ON public.events USING btree (event_type, occurred_at);


--
-- Name: ix_memory_items_content_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_items_content_fts ON public.memory_items USING gin (to_tsvector('simple'::regconfig, COALESCE(content, ''::text)));


--
-- Name: ix_memory_items_scope_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_items_scope_id ON public.memory_items USING btree (scope_id);


--
-- Name: ix_memory_items_scope_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_items_scope_type ON public.memory_items USING btree (scope_type);


--
-- Name: ix_memory_link_pair; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_link_pair ON public.memory_links USING btree (source_id, target_id, relation);


--
-- Name: ix_memory_links_relation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_links_relation ON public.memory_links USING btree (relation);


--
-- Name: ix_memory_links_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_links_source ON public.memory_links USING btree (source_id);


--
-- Name: ix_memory_links_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_links_source_id ON public.memory_links USING btree (source_id);


--
-- Name: ix_memory_links_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_links_target ON public.memory_links USING btree (target_id);


--
-- Name: ix_memory_links_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_links_target_id ON public.memory_links USING btree (target_id);


--
-- Name: ix_memory_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_scope ON public.memory_items USING btree (scope_type, scope_id, memory_type);


--
-- Name: ix_observations_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_observations_process_run_id ON public.observations USING btree (process_run_id);


--
-- Name: ix_os_process_tracks_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_os_process_tracks_process_run_id ON public.os_process_tracks USING btree (process_run_id);


--
-- Name: ix_os_process_tracks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_os_process_tracks_status ON public.os_process_tracks USING btree (status);


--
-- Name: ix_os_process_tracks_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_os_process_tracks_task_id ON public.os_process_tracks USING btree (task_id);


--
-- Name: ix_process_runs_parent_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_process_runs_parent_process_run_id ON public.process_runs USING btree (parent_process_run_id);


--
-- Name: ix_process_runs_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_process_runs_task_id ON public.process_runs USING btree (task_id);


--
-- Name: ix_side_effect_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_side_effect_idempotency ON public.side_effect_receipts USING btree (idempotency_key);


--
-- Name: ix_side_effect_receipts_effect_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_side_effect_receipts_effect_type ON public.side_effect_receipts USING btree (effect_type);


--
-- Name: ix_side_effect_receipts_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_side_effect_receipts_idempotency_key ON public.side_effect_receipts USING btree (idempotency_key);


--
-- Name: ix_steps_process_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_steps_process_run_id ON public.steps USING btree (process_run_id);


--
-- Name: ix_task_intake_decisions_decision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_intake_decisions_decision ON public.task_intake_decisions USING btree (decision);


--
-- Name: ix_task_intake_decisions_request_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_intake_decisions_request_hash ON public.task_intake_decisions USING btree (request_hash);


--
-- Name: ix_task_messages_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_fts ON public.task_messages USING gin (to_tsvector('english'::regconfig, COALESCE(content, ''::text)));


--
-- Name: ix_task_messages_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_kind ON public.task_messages USING btree (kind);


--
-- Name: ix_task_messages_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_session_key ON public.task_messages USING btree (session_key);


--
-- Name: ix_task_messages_slack_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_slack_ts ON public.task_messages USING btree (slack_ts);


--
-- Name: ix_task_messages_task_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_task_created ON public.task_messages USING btree (task_id, created_at);


--
-- Name: ix_task_messages_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_messages_task_id ON public.task_messages USING btree (task_id);


--
-- Name: ix_task_registry_entries_process_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_entries_process_type ON public.task_registry_entries USING btree (process_type);


--
-- Name: ix_task_registry_entries_recurrence_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_entries_recurrence_key ON public.task_registry_entries USING btree (recurrence_key);


--
-- Name: ix_task_registry_entries_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_entries_session_key ON public.task_registry_entries USING btree (session_key);


--
-- Name: ix_task_registry_entries_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_entries_task_id ON public.task_registry_entries USING btree (task_id);


--
-- Name: ix_task_registry_entries_terminal_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_entries_terminal_status ON public.task_registry_entries USING btree (terminal_status);


--
-- Name: ix_task_registry_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_fts ON public.task_registry_entries USING gin (to_tsvector('english'::regconfig, ((COALESCE(intent_snippet, ''::text) || ' '::text) || COALESCE(outcome_summary, ''::text))));


--
-- Name: ix_task_registry_recurrence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_registry_recurrence ON public.task_registry_entries USING btree (recurrence_key, terminal_status);


--
-- Name: ix_tasks_correlation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_correlation_id ON public.tasks USING btree (correlation_id);


--
-- Name: ix_tasks_goal_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_goal_fts ON public.tasks USING gin (to_tsvector('english'::regconfig, (COALESCE(goal, ''::character varying))::text));


--
-- Name: ix_tasks_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_tasks_idempotency_key ON public.tasks USING btree (idempotency_key);


--
-- Name: ix_tasks_intake_decision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_intake_decision_id ON public.tasks USING btree (intake_decision_id);


--
-- Name: ix_tasks_openclaw_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_openclaw_session_key ON public.tasks USING btree (openclaw_session_key);


--
-- Name: ix_tasks_parent_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_parent_task_id ON public.tasks USING btree (parent_task_id);


--
-- Name: ix_tasks_recurrence_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_recurrence_key ON public.tasks USING btree (recurrence_key);


--
-- Name: ix_tasks_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_session ON public.tasks USING btree (openclaw_session_key);


--
-- Name: ix_tasks_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_status ON public.tasks USING btree (status);


--
-- Name: ix_tasks_task_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_task_kind ON public.tasks USING btree (task_kind);


--
-- Name: ix_tasks_task_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_task_type ON public.tasks USING btree (task_type);


--
-- Name: ix_tasks_user_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_user_request_id ON public.tasks USING btree (user_request_id);


--
-- Name: ix_vector_outbox_done_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_outbox_done_at ON public.vector_outbox USING btree (done_at);


--
-- Name: ix_vector_outbox_next_attempt_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_outbox_next_attempt_at ON public.vector_outbox USING btree (next_attempt_at);


--
-- Name: ix_vector_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_outbox_pending ON public.vector_outbox USING btree (next_attempt_at) WHERE (done_at IS NULL);


--
-- Name: ix_vector_outbox_ref_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_vector_outbox_ref_id ON public.vector_outbox USING btree (ref_id);


--
-- Name: ux_dm_ingest_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_dm_ingest_pending ON public.dm_ingest_queue USING btree (kind, ref_id) WHERE (done_at IS NULL);


--
-- Name: ux_dm_links_edge; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_dm_links_edge ON public.dm_links USING btree (source_id, target_id, relation);


--
-- Name: artifacts artifacts_process_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.artifacts
    ADD CONSTRAINT artifacts_process_run_id_fkey FOREIGN KEY (process_run_id) REFERENCES public.process_runs(id);


--
-- Name: dm_chunks dm_chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_chunks
    ADD CONSTRAINT dm_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.dm_documents(id);


--
-- Name: dm_chunks dm_chunks_section_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_chunks
    ADD CONSTRAINT dm_chunks_section_id_fkey FOREIGN KEY (section_id) REFERENCES public.dm_sections(id);


--
-- Name: dm_context_reports dm_context_reports_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_context_reports
    ADD CONSTRAINT dm_context_reports_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: dm_documents dm_documents_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_documents
    ADD CONSTRAINT dm_documents_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: dm_sections dm_sections_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_sections
    ADD CONSTRAINT dm_sections_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.dm_documents(id);


--
-- Name: memory_links memory_links_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.memory_items(id);


--
-- Name: memory_links memory_links_target_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_links
    ADD CONSTRAINT memory_links_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.memory_items(id);


--
-- Name: observations observations_process_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observations
    ADD CONSTRAINT observations_process_run_id_fkey FOREIGN KEY (process_run_id) REFERENCES public.process_runs(id);


--
-- Name: os_process_tracks os_process_tracks_process_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.os_process_tracks
    ADD CONSTRAINT os_process_tracks_process_run_id_fkey FOREIGN KEY (process_run_id) REFERENCES public.process_runs(id);


--
-- Name: os_process_tracks os_process_tracks_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.os_process_tracks
    ADD CONSTRAINT os_process_tracks_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: process_runs process_runs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_runs
    ADD CONSTRAINT process_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: steps steps_process_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.steps
    ADD CONSTRAINT steps_process_run_id_fkey FOREIGN KEY (process_run_id) REFERENCES public.process_runs(id);


--
-- Name: task_messages task_messages_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_messages
    ADD CONSTRAINT task_messages_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: task_registry_entries task_registry_entries_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_registry_entries
    ADD CONSTRAINT task_registry_entries_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- PostgreSQL database dump complete
--


