# Thai Flood Disruption Intelligence

> An end-to-end Data Engineering and Data Science platform for analyzing the potential disruption caused by flooding in Thailand.

---

## 1. Project Overview

Thai Flood Disruption Intelligence is an end-to-end data platform designed to analyze how flooding can affect important locations and infrastructure in Thailand.

Instead of focusing only on predicting whether flooding will occur, the system focuses on answering a more practical question:

> "If flooding occurs, what areas and infrastructure are likely to be affected, and how severe could the disruption be?"

The system combines flood data, geographic data, infrastructure data, and eventually weather and historical information to produce a Flood Disruption Score and identify high-priority areas.

---

## 2. Problem Statement

Flooding is a recurring problem in Thailand that can affect transportation, healthcare, education, logistics, businesses, and local communities.

A flood event itself does not describe the full impact of a disaster.

Two areas may experience similar flooding but have very different consequences depending on:

- Population exposure
- Road accessibility
- Number of hospitals
- Number of schools
- Number of warehouses
- Critical infrastructure
- Historical flood patterns

Therefore, this project aims to build a data-driven system that connects flood events with geographic and infrastructure information to estimate potential disruption.

---

## 3. Main Question

The primary question of this project is:

> "Given a flood event or flood-prone area, what infrastructure and locations are likely to be affected, and which areas should receive higher priority?"

---

## 4. Objectives

### Primary Objectives

1. Collect and integrate flood and geographic data.
2. Build a reliable data ingestion pipeline.
3. Store spatial data using PostgreSQL and PostGIS.
4. Perform geospatial analysis to identify exposed infrastructure.
5. Create a rule-based Flood Disruption Score.
6. Develop a machine learning model to estimate disruption risk.
7. Compare the ML approach against the rule-based baseline.
8. Build an API for serving results.
9. Build an interactive dashboard for visualization.
10. Automate data pipelines using orchestration tools.

---

## 5. Target Users

The system is designed as a decision-support prototype for:

- Local government agencies
- Disaster management teams
- Logistics and warehouse operators
- Businesses
- Infrastructure planners
- Data analysts
- Researchers

The project is not intended to replace official emergency or disaster-management systems.

---

## 6. Current Geographic Scope

### Current Phase 1

The current operational implementation focuses on:

> Pattani, Thailand

Current Phase 1 is limited to building a reliable ingestion pipeline for GISTDA Historical Flood Recurrence data. Infrastructure exposure, disruption scoring, machine learning, APIs, dashboards, and production orchestration remain part of the long-term roadmap rather than the current acceptance criteria.

### Future Expansion

After the Pattani ingestion pipeline and later analytical stages are validated:

1. Additional selected provinces, including Pathum Thani and Bangkok
2. Additional provinces
3. Nationwide Thailand

---

## 7. Data Requirements

The project will require several categories of data.

### 7.1 Flood Data

Potential fields:

- Flood event ID
- Date / timestamp
- Location
- Flooded area
- Flood geometry
- Flood severity
- Historical flood information

---

### 7.2 Infrastructure Data

Potential infrastructure:

- Hospitals
- Schools
- Roads
- Warehouses
- Important public facilities

Potential fields:

- ID
- Name
- Type
- Province
- District
- Latitude
- Longitude
- Geometry

---

### 7.3 Geographic Data

Potential data:

- Province boundaries
- District boundaries
- Subdistrict boundaries
- Road networks
- Water bodies
- Elevation

---

### 7.4 Weather Data

Weather data will initially be considered an optional/secondary data source.

Potential variables:

- Rainfall
- Temperature
- Humidity
- Wind speed
- Water level

Weather data can later be used as features for the ML component.

---

## 8. Core System Concept

The system consists of two major analytical components.

### A. Flood Exposure

Measures how much an area or infrastructure is geographically exposed to flooding.

Examples:

- Infrastructure inside flooded polygons
- Distance from flood areas
- Flooded area percentage
- Population exposed

---

### B. Disruption Impact

Measures the potential consequence of the exposure.

Examples:

- Number of hospitals affected
- Number of schools affected
- Number of roads affected
- Number of warehouses affected
- Population affected
- Infrastructure criticality

---

## 9. Flood Disruption Score

The initial system will use a rule-based scoring approach as a baseline.

Example:

Disruption Score =

    40% Flood Exposure
  + 30% Infrastructure Exposure
  + 20% Population Exposure
  + 10% Historical Flood Risk

The exact weighting is not final and must be evaluated during development.

The baseline exists so that the machine learning model can later be compared against a transparent non-ML approach.

---

## 10. Machine Learning Problem

The ML component will not simply predict "Flood / No Flood."

The initial objective is to investigate whether machine learning can improve the estimation or ranking of disruption risk.

Potential target:

> Probability that an area will experience significant disruption.

Potential features:

- Flood area
- Distance to flood boundary
- Historical flood frequency
- Number of hospitals
- Number of schools
- Number of roads
- Population exposure
- Elevation
- Rainfall
- Water level
- Geographic characteristics

Potential models:

1. Baseline
2. Logistic Regression
3. Random Forest
4. XGBoost
5. LightGBM

The final model will be selected based on appropriate evaluation metrics rather than model popularity.

---

## 11. Geospatial Analysis

Geospatial analysis is a core component of this project.

The system should support operations such as:

### Spatial Join

Identify infrastructure located inside flood areas.

### Distance Analysis

Calculate the distance between infrastructure and flood boundaries.

### Intersection

Determine which roads intersect flooded areas.

### Exposure Analysis

Calculate how much of an area or infrastructure network is exposed.

### Priority Mapping

Rank locations based on their estimated disruption risk.

---

## 12. Data Engineering Architecture

The target architecture is:

Source Data
    ↓
Data Ingestion
    ↓
Raw Data Layer
    ↓
Data Validation
    ↓
PostgreSQL + PostGIS
    ↓
dbt Transformation
    ↓
Feature Tables
    ↓
Analytics / ML
    ↓
Disruption Engine
    ↓
FastAPI
    ↓
Dashboard

---

## 13. Technology Stack

### Programming

- Python
- SQL

### Database

- PostgreSQL
- PostGIS

### Data Processing

- Pandas
- GeoPandas
- Polars (optional)

### Data Transformation

- dbt

### Orchestration

- Apache Airflow

### Machine Learning

- scikit-learn
- XGBoost
- LightGBM

### Explainability

- SHAP

### API

- FastAPI

### Visualization

- Streamlit
- Plotly
- Folium / PyDeck

### Infrastructure

- Docker
- Docker Compose

### Version Control

- Git
- GitHub

---

## 14. Data Pipeline

The target automated pipeline is:

    Extract
       ↓
    Validate
       ↓
    Load
       ↓
    Transform
       ↓
    Test
       ↓
    Feature Engineering
       ↓
    Prediction
       ↓
    Update Results

Apache Airflow will eventually orchestrate this workflow.

---

## 15. Data Quality

The pipeline must validate incoming data before it reaches analytical tables.

Examples:

- Missing required fields
- Invalid coordinates
- Duplicate records
- Invalid geometry
- Invalid dates
- Negative flood area
- Invalid administrative boundaries
- Schema changes

A failed data-quality check should prevent invalid data from silently entering downstream analysis.

---

## 16. Expected Outputs

The system should eventually provide:

### Area-level

- Flood risk
- Flood exposure
- Disruption score
- Priority level

### Infrastructure-level

- Affected hospitals
- Affected schools
- Affected roads
- Affected warehouses
- Distance to flood area

### Geographic

- Flood map
- Exposure map
- Risk map
- Priority map

### Analytical

- Risk factors
- Historical trends
- Model performance
- Feature importance

---

## 17. Dashboard

The dashboard should provide:

### Overview

- Current selected area
- Overall disruption score
- Risk level
- Number of affected infrastructure

### Map

Interactive map containing:

- Flood areas
- Roads
- Hospitals
- Schools
- Warehouses
- Risk areas

### Ranking

Top areas by:

- Flood exposure
- Infrastructure exposure
- Disruption score

### Explanation

Show major factors contributing to the risk.

---

## 18. API

The backend should eventually provide endpoints such as:

GET /areas

GET /areas/{area_id}

GET /flood-events

GET /risk

GET /infrastructure/affected

GET /map-data

GET /prediction

The exact API design will be finalized after the database schema is implemented.

---

## 19. Evaluation

The project will evaluate both the data system and ML component.

### Data Engineering

- Pipeline reliability
- Data freshness
- Data quality
- Processing time
- Failure handling

### Geospatial

- Correct spatial joins
- Correct distance calculations
- Correct exposure calculations

### Machine Learning

Depending on the final target:

- ROC-AUC
- Precision
- Recall
- F1
- PR-AUC
- Calibration
- Ranking quality

The ML model must be compared against the rule-based baseline.

---

## 20. Long-Term MVP Definition

The criteria in this section describe the broader disruption-intelligence MVP. They are not the acceptance criteria for the current operational Phase 1.

The MVP is considered complete when the system can:

1. Load real flood data.
2. Load geographic boundaries.
3. Load infrastructure locations.
4. Store spatial data in PostgreSQL/PostGIS.
5. Identify infrastructure exposed to flood areas.
6. Calculate a basic Disruption Score.
7. Display results on an interactive map.
8. Produce a ranked list of high-priority areas.

Machine learning, Airflow, dbt, FastAPI, and Docker can initially be implemented after the core MVP works.

---

## 21. Development Phases

The phases below are the long-term development roadmap. For the current implementation, "Phase 1" refers specifically to the Pattani GISTDA Historical Flood Recurrence ingestion pipeline described in Section 6 and the repository README. The broader roadmap is preserved for future development.

### Phase 0 — Specification

- Define problem
- Define scope
- Define users
- Define outputs

### Phase 1 — Data Discovery

- Find data sources
- Inspect formats
- Evaluate data quality
- Build data inventory

### Phase 2 — Data Foundation

- PostgreSQL
- PostGIS
- Initial schema
- Load raw data

### Phase 3 — Geospatial MVP

- Spatial joins
- Distance analysis
- Exposure analysis
- Basic risk score

### Phase 4 — Data Engineering

- Data ingestion scripts
- Data validation
- dbt
- Airflow

### Phase 5 — Machine Learning

- Feature engineering
- Baseline
- Model training
- Evaluation
- Explainability

### Phase 6 — API

- FastAPI
- Prediction endpoints
- Geographic endpoints

### Phase 7 — Dashboard

- Interactive map
- Risk dashboard
- Infrastructure analysis
- Model explanation

### Phase 8 — Productionization

- Docker
- Logging
- Error handling
- Testing
- Monitoring
- Documentation

---

## 22. Non-Goals

The project will NOT attempt to:

- Replace official flood warning systems.
- Provide emergency instructions.
- Guarantee flood predictions.
- Simulate complete hydrological behavior.
- Build a nationwide production disaster-management platform in the MVP.

The focus is:

> Data Engineering + Geospatial Analytics + Machine Learning + Decision Support

---

## 23. Final Project Goal

The final system should demonstrate that a data scientist / data engineer can:

1. Work with real-world Thai data.
2. Build reliable data pipelines.
3. Work with spatial databases.
4. Perform geospatial analysis.
5. Build and evaluate machine learning models.
6. Compare ML with a transparent baseline.
7. Build APIs.
8. Build an analytical dashboard.
9. Containerize and deploy a data product.
10. Communicate analytical results to decision makers.

---

## 24. Portfolio Statement

> Thai Flood Disruption Intelligence is an end-to-end data platform that integrates flood, geographic, infrastructure, and historical data to identify areas and critical infrastructure exposed to flooding. The system combines data engineering, geospatial analytics, machine learning, and decision-support visualization to estimate and prioritize potential disruption in Thailand.
