---
name: data-engineering
description: Use when building data pipelines with Apache Airflow, dbt transformations, Spark optimization, and data quality validation with Great Expectations. Covers orchestration, transformation, processing, and quality frameworks.
summary: Data pipelines with Airflow DAGs, dbt transformations, Spark optimization, and Great Expectations data quality validation.
triggers: [data pipeline, ETL, Airflow, dbt, Spark, data quality, Great Expectations, orchestration, transformation]
disable-model-invocation: true

---
# Data Engineering (Unified)

## Goal
Build reliable, scalable data pipelines with proper orchestration, transformation patterns, processing optimization, and quality validation.

## When to Use
- Creating data pipeline orchestration with Airflow
- Building data transformations with dbt
- Optimizing Spark jobs for large datasets
- Implementing data quality validation
- Designing ETL/ELT workflows
- Setting up analytics engineering practices

## Apache Airflow (Orchestration)

Airflow is the task orchestrator for determining when, how, and with what parameters to run each task.

### Environments

| Environment | Purpose                        |
| ----------- | ------------------------------ |
| Production  | Production workflows           |
| Staging     | Pre-production testing         |
| Test        | Platform upgrades & testing    |

**Note:** Staging environments typically reset periodically. Reapply staging branch changes if not yet merged to main.

### DAG Synchronization

- DAGs stored in `airflow-dags` repository
- Synchronized to all Airflow nodes (scheduler, workers, web)
- All nodes run on Kubernetes
- DAG module imported every 30 seconds
- Code synchronized independently to each node

### DAG Design Principles

| Principle       | Description                         |
| --------------- | ----------------------------------- |
| **Idempotent**  | Running twice produces same result  |
| **Atomic**      | Tasks succeed or fail completely    |
| **Incremental** | Process only new/changed data       |
| **Observable**  | Logs, metrics, alerts at every step |
| **Static**      | Task organization static per environment |

### Basic DAG Structure
```python
from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.empty import EmptyOperator

default_args = {
    'owner': 'data-team',
    'depends_on_past': False,
    'email_on_failure': True,
    'retries': 3,
    'retry_delay': timedelta(minutes=5),
    'retry_exponential_backoff': True,
}

with DAG(
    dag_id='example_etl',
    default_args=default_args,
    description='Example ETL pipeline',
    schedule='0 6 * * *',  # Daily at 6 AM
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=['etl'],
    max_active_runs=1,
) as dag:
    start = EmptyOperator(task_id='start')
    
    def extract_data(**context):
        execution_date = context['ds']
        return {'records': 1000}
    
    extract = PythonOperator(
        task_id='extract',
        python_callable=extract_data,
    )
    
    end = EmptyOperator(task_id='end')
    start >> extract >> end
```

### TaskFlow API (Airflow 2.0+)
```python
from airflow.decorators import dag, task

@dag(
    dag_id='taskflow_etl',
    schedule='@daily',
    start_date=datetime(2024, 1, 1),
    catchup=False,
)
def taskflow_etl():
    @task()
    def extract(source: str) -> dict:
        df = pd.read_csv(f's3://bucket/{source}/{{ ds }}.csv')
        return {'data': df.to_dict(), 'rows': len(df)}

    @task()
    def transform(extracted: dict) -> dict:
        df = pd.DataFrame(extracted['data'])
        df['processed_at'] = datetime.now()
        return {'data': df.to_dict(), 'rows': len(df)}

    @task()
    def load(transformed: dict, target: str):
        df = pd.DataFrame(transformed['data'])
        df.to_parquet(f's3://bucket/{target}/{{ ds }}.parquet')
        return transformed['rows']

    # XCom passing with dependencies
    extracted = extract(source='raw_data')
    transformed = transform(extracted)
    load(transformed, target='processed_data')

taskflow_etl()
```

### Task Dependencies
```python
# Linear
task1 >> task2 >> task3

# Fan-out
task1 >> [task2, task3, task4]

# Fan-in
[task1, task2, task3] >> task4

# Complex
task1 >> task2 >> task4
task1 >> task3 >> task4
```

### DAG Development Best Practices

#### Avoid Dynamic Task Generation
- Task organization should be **static per environment**
- ❌ **Avoid:** Per-client loops or dynamic task generation
- ✅ **Why:** Each task adds scheduler load; dynamic tasks don't scale

#### Lightweight DAG Imports
- DAGs imported every 30 seconds on every node
- ❌ **Avoid at top level:**
  - Database queries
  - Airflow model class usage (previous task instances)
  - Variable/Connection retrieval
  - XCom pulls
- ✅ **Only inside tasks:** All database operations

#### Templating for Idempotency
Use Jinja templating with runtime variables for idempotent DAGs:

```python
from airflow.decorators import task

@task()
def process_data(**context):
    # Use templated variables for idempotency
    execution_date = context['ds']  # '2024-01-15'
    logical_date = context['execution_date']
    
    # Process data for this specific date
    data = fetch_data(date=execution_date)
    return process(data)
```

**Available template variables:**
- `{{ ds }}` - Execution date (YYYY-MM-DD)
- `{{ execution_date }}` - Logical date for scheduled interval
- `{{ dag_run.conf }}` - DAG run configuration
- Full list: [Airflow Variables Reference](https://airflow.apache.org/docs/apache-airflow/stable/templates-ref.html)

#### Job Metadata Pattern

```python
from datetime import datetime
from airflow import DAG

# Define job metadata for ownership and alerting
IMPORTANCE_CRITICAL = "critical"
TEAM_OWNER = "data-team"

def make_dag():
    dag = DAG(
        dag_id='document_shredder',
        schedule_interval='0 8 * * *',
        catchup=False,
        default_args={
            'start_date': datetime(2021, 2, 24),
            'retries': 1,
            'owner': TEAM_OWNER,
        },
        tags=[IMPORTANCE_CRITICAL, TEAM_OWNER],
    )
    return dag
```

**Importance Levels:**

| Level         | Paging | Alert Channel               |
| ------------- | ------ | --------------------------- |
| BEST_EFFORT   | No     | General notifications       |
| NORMAL        | Yes    | General notifications       |
| HIGH          | Yes    | Priority notifications      |
| CRITICAL      | Yes    | Critical alerts             |

#### Triggering Other DAGs

Use `TriggerDagRunOperator` to trigger downstream DAGs:

```python
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

trigger_task = TriggerDagRunOperator(
    task_id='trigger_downstream',
    trigger_dag_id='downstream_dag',
    conf={'key': 'value'},
)
```

### Testing Workflows

#### Local Testing

See `airflow-dags` repository README for local development with Docker Compose.

#### Testing in Staging (Databricks)

Use `branch` and `image_tag` parameters to test unmerged code in staging Databricks workspace:

**1. Test latest commit on a branch:**
```python
from airflow.providers.databricks.operators.databricks import DatabricksSubmitRunOperator

task_foo = DatabricksSubmitRunOperator(
    task_id='task_foo_id',
    dag=dag,
    json={
        'new_cluster': {...},
        'spark_python_task': {
            'python_file': 's3://bucket/path/to/script.py',
            'parameters': ['--branch', 'feature_branch'],
        }
    }
)
```

**2. Test specific commit on a branch:**
```python
task_baz = DatabricksJobOperator(
    task_id='task_baz_id',
    dag=dag,
    job_args=build_dbx_job_args,
    image_repo=DatabricksImageRepo.PYSPARK_CALCULATION_JOBS,
    package_name='pyspark_baz',
    module=f'{BASE_MODULE_PATH}.bar',
    branch='DAT-5678/baz_feature_branch',
    image_tag='083f24cfd6cee271e0f9b7341d0249a8612d1e6b',  # Full 40-char SHA
)
```

**3. Test specific commit on main:**
```python
task_bar = DatabricksJobOperator(
    task_id='task_bar_id',
    dag=dag,
    job_args=build_dbx_job_args,
    image=DatabricksImage.PYSPARK_CALCULATION_JOBS,
    package_name='pyspark_bar',
    module=f'{BASE_MODULE_PATH}.bar',
    image_tag='083f24cfd6cee271e0f9b7341d0249a8612d1e6b',  # Commit on main
)
```

**Parameter Rules:**
- `branch`: Latest commit on that branch (must be built and pushed to ECR)
- `image_tag`: Specific commit SHA (use full 40-character SHA)
- `branch` + `image_tag`: Specific commit on a branch
- `image_tag` alone: Commit from main branch

#### Runtime Configurations

**⚠️ Production Restriction:** Runtime parameters are **NOT allowed in production** (for logging and review).

**Staging/Local Only:** Pass configurations via DAG conf (UI or JSON):

```json
{
  "task_foo_id": {"branch": "DAT-1234/feature"},
  "task_bar_id": {"image_tag": "083f24cfd6cee271e0f9b7341d0249a8612d1e6b"},
  "task_baz_id": {
    "branch": "DAT-5678/feature",
    "image_tag": "083f24cfd6cee271e0f9b7341d0249a8612d1e6b"
  }
}
```

**Apply to all tasks:**
```json
{
  "all_tasks": {"branch": "DAT-1234/feature"}
}
```

**Precedence (highest to lowest):**
1. `task_id` in DAG conf
2. `all_tasks` in DAG conf  
3. Hardcoded values in code

**Notes:**
- DAG conf overrides hardcoded `branch`/`image_tag`
- `image_tag` without `branch` defaults to main ECR repo
- For older commit on branch, pass both `branch` and `image_tag`

### Common Configuration Options

| Option             | Configured On | Description                                           |
| ------------------ | ------------- | ----------------------------------------------------- |
| max_active_runs    | DAG           | Max simultaneous DAG runs                             |
| max_active_tasks   | DAG           | Max tasks running simultaneously across DAG           |
| start_date         | DAG           | Logical date when Airflow starts running DAG (constant)|
| execution_date     | DAG           | Logical start of current scheduled interval (per run) |
| catchup            | DAG           | Run all past intervals if True                        |
| concurrency        | Task          | Max instances of same Task running simultaneously     |
| depends_on_past    | Task          | Requires previous DAG run's task to complete first    |
| retries            | Task          | Number of retries before failing                      |
| retry_delay        | Task          | Delay between retries                                 |

**Important Notes:**
- `start_date` is **constant** for all DAG runs (do not automate)
- If `start_date` is in past and `catchup=True`, Airflow runs all intervals
- `execution_date` **changes per DAG run** and is passed to every task

## dbt (Transformation)

### Model Layers (Medallion Architecture)
```
sources/          Raw data definitions
    ↓
staging/          1:1 with source, light cleaning
    ↓
intermediate/     Business logic, joins, aggregations
    ↓
marts/            Final analytics tables
```

### Naming Conventions

| Layer        | Prefix         | Example                       |
| ------------ | -------------- | ----------------------------- |
| Staging      | `stg_`         | `stg_stripe__payments`        |
| Intermediate | `int_`         | `int_payments_pivoted`        |
| Marts        | `dim_`, `fct_` | `dim_customers`, `fct_orders` |

### Project Structure
```
models/
├── staging/
│   ├── stripe/
│   │   ├── _stripe__sources.yml
│   │   ├── _stripe__models.yml
│   │   └── stg_stripe__payments.sql
│   └── shopify/
│       └── stg_shopify__orders.sql
├── intermediate/
│   └── finance/
│       └── int_payments_pivoted.sql
└── marts/
    ├── core/
    │   ├── dim_customers.sql
    │   └── fct_orders.sql
    └── finance/
        └── fct_revenue.sql
```

### Source Definition
```yaml
# models/staging/stripe/_stripe__sources.yml
version: 2
sources:
  - name: stripe
    database: raw
    schema: stripe
    freshness:
      warn_after: {count: 12, period: hour}
      error_after: {count: 24, period: hour}
    tables:
      - name: payments
        columns:
          - name: id
            tests:
              - unique
              - not_null
```

### Staging Model
```sql
-- models/staging/stripe/stg_stripe__payments.sql
with source as (
    select * from {{ source('stripe', 'payments') }}
),
renamed as (
    select
        id as payment_id,
        customer_id,
        amount / 100.0 as amount,  -- cents to dollars
        status as payment_status,
        created as created_at,
        _fivetran_synced as _loaded_at
    from source
)
select * from renamed
```

### Incremental Model
```sql
{{
    config(
        materialized='incremental',
        unique_key='payment_id',
        on_schema_change='append_new_columns'
    )
}}

with source as (
    select * from {{ source('stripe', 'payments') }}
    {% if is_incremental() %}
    where _fivetran_synced > (select max(_loaded_at) from {{ this }})
    {% endif %}
)
select * from source
```

## Apache Spark (Processing)

### Key Performance Factors

| Factor            | Impact                | Solution                      |
| ----------------- | --------------------- | ----------------------------- |
| **Shuffle**       | Network I/O, disk I/O | Minimize wide transformations |
| **Data Skew**     | Uneven task duration  | Salting, broadcast joins      |
| **Serialization** | CPU overhead          | Use Kryo, columnar formats    |
| **Memory**        | GC pressure, spills   | Tune executor memory          |
| **Partitions**    | Parallelism           | Right-size partitions         |

### Optimized Spark Session
```python
from pyspark.sql import SparkSession

spark = (SparkSession.builder
    .appName("OptimizedJob")
    .config("spark.sql.adaptive.enabled", "true")
    .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
    .config("spark.sql.adaptive.skewJoin.enabled", "true")
    .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
    .config("spark.sql.shuffle.partitions", "200")
    .getOrCreate())
```

### Partition Optimization
```python
# Calculate optimal partition count
# Target: 128MB - 256MB per partition
def calculate_partitions(data_size_gb: float, partition_size_mb: int = 128) -> int:
    return max(int(data_size_gb * 1024 / partition_size_mb), 1)

# Repartition for even distribution
df_repartitioned = df.repartition(200, "partition_key")

# Coalesce to reduce partitions (no shuffle)
df_coalesced = df.coalesce(100)

# Write with partitioning
df.write.partitionBy("year", "month", "day").parquet("output/")
```

### Join Optimization
```python
from pyspark.sql import functions as F

# 1. Broadcast Join - Small table (<10MB)
result = large_df.join(
    F.broadcast(small_df),
    on="key",
    how="left"
)

# 2. Bucket Join - Pre-sorted, no shuffle
df.write.bucketBy(200, "customer_id").sortBy("customer_id").saveAsTable("bucketed_table")

# 3. Skew handling - Enable AQE
spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
```

## Data Quality (Great Expectations)

### Great Expectations Integration

Great Expectations provides a framework for data validation with common use cases.

#### Quality Check Patterns

| Pattern                         | Use Case                  |
| ------------------------------- | ------------------------- |
| Spark Expectations              | Data Lake via Databricks  |
| SQL Expectations                | Postgres, Redshift, etc.  |

**Spark/Databricks Example:**
```python
import great_expectations as gx

# Initialize context
context = gx.get_context()

# Create expectation suite
suite = context.add_expectation_suite("orders_suite")

# Add expectations
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id")
)

# Run validation
results = context.run_checkpoint(checkpoint_name="orders_checkpoint")
```

**SQL/Postgres Example:**
```python
import great_expectations as gx

# Initialize context
context = gx.get_context()

# Create SQL datasource
datasource = context.sources.add_postgres(
    name="my_postgres",
    connection_string="postgresql://..."
)

# Add expectations
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeUnique(column="customer_id")
)
```

#### Prebuilt Expectations

Browse expectations at: https://greatexpectations.io/expectations

**⚠️ Filter by platform:** spark, postgres, or redshift

**Experimental Expectations:**
```python
# Import from separate package
from great_expectations_experimental.expectations.expect_column_values_to_match_thai import ExpectColumnValuesToMatchThai
```

**Performance Note:** Community Pandas expectations are not performant on Databricks and are not supported by this package.

### Quality Dimensions

| Dimension        | Description              | Example Check                         |
| ---------------- | ------------------------ | ------------------------------------- |
| **Completeness** | No missing values        | `expect_column_values_to_not_be_null` |
| **Uniqueness**   | No duplicates            | `expect_column_values_to_be_unique`   |
| **Validity**     | Values in expected range | `expect_column_values_to_be_in_set`   |
| **Accuracy**     | Data matches reality     | Cross-reference validation            |
| **Timeliness**   | Data is recent           | `expect_column_max_to_be_between`     |

### Expectation Suite
```python
import great_expectations as gx

context = gx.get_context()
suite = context.add_expectation_suite("orders_suite")

# Schema
suite.add_expectation(
    gx.expectations.ExpectTableColumnsToMatchSet(
        column_set=["order_id", "customer_id", "amount", "status"]
    )
)

# Primary key
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToNotBeNull(column="order_id")
)
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeUnique(column="order_id")
)

# Categorical values
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeInSet(
        column="status",
        value_set=["pending", "shipped", "delivered", "cancelled"]
    )
)

# Numeric range
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeBetween(
        column="amount",
        min_value=0,
        max_value=100000
    )
)
```

### dbt Tests
```yaml
# models/marts/core/_core__models.yml
version: 2
models:
  - name: fct_orders
    columns:
      - name: order_id
        tests:
          - unique
          - not_null
      - name: customer_id
        tests:
          - not_null
          - relationships:
              to: ref('dim_customers')
              field: customer_id
      - name: status
        tests:
          - accepted_values:
              values: ['pending', 'shipped', 'delivered', 'cancelled']
```

## Implementation Checklist

### Airflow
- [ ] DAGs follow idempotency principles (same inputs = same result)
- [ ] Task organization is static (no dynamic/per-client loops)
- [ ] No database queries at top level (only inside tasks)
- [ ] Templating used for runtime variables
- [ ] JobMetadata configured with team and importance level
- [ ] TriggerDagRunOperator used for cross-DAG triggers
- [ ] start_date is constant, catchup set appropriately
- [ ] TaskFlow API used for Python-heavy workflows
- [ ] Databricks testing uses branch/image_tag parameters in staging

### dbt
- [ ] Models organized into staging/intermediate/marts
- [ ] Source freshness checks configured
- [ ] Incremental models for large tables
- [ ] dbt tests cover primary keys and relationships

### Spark
- [ ] Spark jobs use AQE and optimized joins
- [ ] Partition sizes optimized (128MB-256MB)

### Data Quality
- [ ] Great Expectations used for data quality validation
- [ ] Expectations filtered by platform (spark/postgres/redshift)
- [ ] Quality checks in pipeline
- [ ] Pipeline monitoring and alerting in place
