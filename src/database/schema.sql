CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA pattani_exposure;

CREATE TABLE pattani_exposure.schema_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    schema_version text NOT NULL CHECK (schema_version = '1.0')
);
INSERT INTO pattani_exposure.schema_metadata (schema_version) VALUES ('1.0');

CREATE TABLE pattani_exposure.report_versions (
    report_id text PRIMARY KEY CHECK (report_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    schema_version text NOT NULL,
    report_version text NOT NULL,
    policy_label text NOT NULL CHECK (policy_label = 'exploratory_non_authoritative'),
    crs_status text NOT NULL CHECK (crs_status = 'provider_unverified_exploratory_interpretation'),
    manifest_relative_path text NOT NULL,
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    interpretation_scope text NOT NULL,
    caveats text[] NOT NULL,
    loaded_at_utc timestamptz NOT NULL DEFAULT current_timestamp
);

CREATE TABLE pattani_exposure.exposure_headline (
    report_id text NOT NULL REFERENCES pattani_exposure.report_versions(report_id),
    infrastructure_type text NOT NULL CHECK (infrastructure_type IN ('roads', 'healthcare')),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    exposed_count bigint NOT NULL CHECK (exposed_count >= 0),
    non_exposed_count bigint NOT NULL CHECK (non_exposed_count >= 0),
    PRIMARY KEY (report_id, infrastructure_type),
    CHECK (total_count = exposed_count + non_exposed_count)
);

CREATE TABLE pattani_exposure.annual_exposure (
    report_id text NOT NULL REFERENCES pattani_exposure.report_versions(report_id),
    year smallint NOT NULL CHECK (year BETWEEN 2011 AND 2024),
    infrastructure_type text NOT NULL CHECK (infrastructure_type IN ('roads', 'healthcare')),
    exposed_count bigint NOT NULL CHECK (exposed_count >= 0),
    PRIMARY KEY (report_id, year, infrastructure_type)
);

CREATE TABLE pattani_exposure.road_category_exposure (
    report_id text NOT NULL REFERENCES pattani_exposure.report_versions(report_id),
    road_category text NOT NULL CHECK (length(road_category) BETWEEN 1 AND 100),
    total_count bigint NOT NULL CHECK (total_count >= 0),
    exposed_count bigint NOT NULL CHECK (exposed_count >= 0 AND exposed_count <= total_count),
    PRIMARY KEY (report_id, road_category)
);

CREATE TABLE pattani_exposure.frequency_consistency (
    report_id text PRIMARY KEY REFERENCES pattani_exposure.report_versions(report_id),
    feature_count bigint NOT NULL CHECK (feature_count >= 0),
    match_count bigint NOT NULL CHECK (match_count >= 0),
    mismatch_count bigint NOT NULL CHECK (mismatch_count >= 0),
    missing_or_invalid_count bigint NOT NULL CHECK (missing_or_invalid_count >= 0),
    relationship_status text NOT NULL CHECK (relationship_status = 'observed_structural_relationship_only'),
    CHECK (feature_count = match_count + mismatch_count + missing_or_invalid_count)
);

CREATE FUNCTION pattani_exposure.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable aggregate record';
END;
$$;

CREATE TRIGGER report_versions_immutable BEFORE UPDATE OR DELETE ON pattani_exposure.report_versions
FOR EACH ROW EXECUTE FUNCTION pattani_exposure.reject_mutation();
CREATE TRIGGER exposure_headline_immutable BEFORE UPDATE OR DELETE ON pattani_exposure.exposure_headline
FOR EACH ROW EXECUTE FUNCTION pattani_exposure.reject_mutation();
CREATE TRIGGER annual_exposure_immutable BEFORE UPDATE OR DELETE ON pattani_exposure.annual_exposure
FOR EACH ROW EXECUTE FUNCTION pattani_exposure.reject_mutation();
CREATE TRIGGER road_category_exposure_immutable BEFORE UPDATE OR DELETE ON pattani_exposure.road_category_exposure
FOR EACH ROW EXECUTE FUNCTION pattani_exposure.reject_mutation();
CREATE TRIGGER frequency_consistency_immutable BEFORE UPDATE OR DELETE ON pattani_exposure.frequency_consistency
FOR EACH ROW EXECUTE FUNCTION pattani_exposure.reject_mutation();
