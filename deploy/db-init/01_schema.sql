--
-- PostgreSQL database dump
--


-- Dumped from database version 18.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: Batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Batches" (
    "BatchNo" bigint,
    "TimeStamp" timestamp without time zone,
    "Plant Name" text,
    "Recipe Name" text,
    "Start Date Time" text,
    "End Date Time" text,
    "Total Batch Weight" double precision
);


--
-- Name: Data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Data" (
    "Name" text,
    "Category" text,
    "Tag_name" text,
    "Data_type" text,
    "Sample_mode" text,
    "Trigger" text
);


--
-- Name: Info_db; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Info_db" (
    "Id" bigint,
    "Particulars" text,
    "Info" text
);


--
-- Name: MaterialData; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."MaterialData" (
    "SiloNo" double precision,
    "MaterialName" text,
    "MaterialCode" text,
    "OperatorName" text,
    "TotalExtracted" text
);


--
-- Name: RecipeTagName; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."RecipeTagName" (
    "Name" text,
    "SiloNo" text,
    "Tag_name" text
);


--
-- Name: plc_data_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.plc_data_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: plc_data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plc_data (
    "Id" bigint DEFAULT nextval('public.plc_data_id_seq'::regclass),
    "TimeStamp" timestamp without time zone,
    "Name" text,
    "Category" text,
    "DataType" text,
    "Value" text,
    "BatchNo" bigint,
    "DailyBatchNo" text
);


--
-- Name: recipeData; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."recipeData" (
    "Index" bigint,
    "SiloNo" bigint,
    "MaterialName" text,
    "SetWeight" bigint,
    "FineWeight" bigint,
    "Tolerance" bigint,
    "Category" text,
    "CoarseSpeed" double precision,
    "FineSpeed" double precision
);


--
-- Name: recipes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recipes (
    id bigint,
    category text,
    name text
);


--
-- Name: sqlite_sequence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sqlite_sequence (
    name text,
    seq bigint
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id bigint,
    username text,
    password_hash text,
    role text,
    user_access text,
    is_active bigint,
    last_login text
);


--
-- Name: ix_batches_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_batches_timestamp ON public."Batches" USING btree ("TimeStamp");


--
-- Name: ix_plc_data_batchno; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plc_data_batchno ON public.plc_data USING btree ("BatchNo");


--
-- Name: ix_plc_data_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_plc_data_timestamp ON public.plc_data USING btree ("TimeStamp");


--
-- PostgreSQL database dump complete
--


