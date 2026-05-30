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
