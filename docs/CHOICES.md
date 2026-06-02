# Engineering Choices

This document explains *why* architectural decisions were made.

## 1. Linting and Formatting

* **Decision**: Use `ruff`.
* **Alternatives considered**: `black` + `flake8` + `isort`.
* **Pros**: Extremely fast, consolidates multiple tools into one, easy to configure.
* **Cons**: Relatively new (though highly stable now), might miss some obscure flake8 plugins.
* **Final choice**: `ruff`.
* **Reasoning**: Speed and simplicity. For a challenge submission, having a single fast tool reduces cognitive load and keeps the setup lightweight. We are omitting `mypy` initially to keep the barrier low, but we can add it later if strict type checking becomes necessary.

## 2. Dependency Management

* **Decision**: `uv` with `requirements.txt`.
* **Alternatives considered**: `Poetry`, `pipenv`, standard `pip`.
* **Pros**: `uv` is extremely fast and compatible with standard pip workflows. `requirements.txt` is universally understood by evaluators.
* **Cons**: Lacks the rich dependency resolution features of Poetry out of the box (unless using `uv pip compile`).
* **Final choice**: `uv` + `requirements.txt`.
* **Reasoning**: Simplicity and evaluator friendliness. We want the reviewers to be able to jump in easily without needing to learn or install complex environment managers if they don't want to.

## 3. Database

* **Decision**: `SQLite`.
* **Alternatives considered**: `PostgreSQL`.
* **Pros**: Zero infrastructure setup, easy to bundle, sufficient for MVP and testing.
* **Cons**: Not suitable for high concurrency production loads.
* **Final choice**: `SQLite` (initially).
* **Reasoning**: Follows the "production-readiness over flashy features" principle. We will implement the Repository Pattern to abstract the data layer, allowing a seamless transition to PostgreSQL when the system scales.

## 4. Framework Selection

* **Decision**: FastAPI
* **Alternatives considered**: Flask, Django
* **Pros**: 
  - Native asynchronous support for high-performance I/O operations (crucial for an event-driven intelligence system).
  - Strong typing with Pydantic ensures rigorous request/response validation at the API boundary.
  - Automatic OpenAPI (Swagger) documentation generation, significantly improving developer experience and API discoverability.
* **Cons**: Smaller ecosystem for "batteries-included" features (e.g., built-in admin panel) compared to Django.
* **Final choice**: FastAPI
* **Reasoning**: The Store Intelligence System requires high throughput for event processing and clean, validated API contracts. FastAPI's first-class integration with Python type hints and Pydantic provides robust, maintainable code. Furthermore, its asynchronous capabilities ensure the API can handle high concurrency gracefully without the heavy boilerplate of Django or the manual typing enforcement needed in Flask.

## 5. Configuration and Logging

* **Decision**: `structlog` for logging, `pydantic-settings` for configuration.
* **Alternatives considered**: Standard Python `logging`, `python-decouple`, standard `os.environ`.
* **Pros**: 
  - **Structlog**: Outputs logs as JSON, which is essential for modern log aggregators (e.g., ELK stack, Datadog) to parse events properly. Context variables allow attaching request IDs to all logs effortlessly.
  - **Environment-based config (pydantic-settings)**: Adheres to the 12-Factor App methodology. Validates configuration at startup using Pydantic, failing fast if required variables (like `DATABASE_URL`) are missing or incorrectly typed.
* **Cons**: Introduces external dependencies for features built into the Python standard library.
* **Final choice**: `structlog` and `pydantic-settings`.
* **Reasoning**: A production-grade system needs structured logging to track business intelligence events effectively and debug issues across services. `structlog` solves this elegantly. For configuration, environment variables are the standard for Dockerized deployments. `pydantic-settings` ensures type safety and rigorous validation, preventing runtime crashes due to misconfiguration.

## 6. Database Foundation

* **Decision**: SQLite (initial) paired with SQLAlchemy 2.0 ORM.
* **Alternatives considered**: PostgreSQL, Raw SQL (aiosqlite/psycopg), Tortoise ORM, SQLModel.
* **Pros**: 
  - **SQLite**: Requires zero operational overhead, making it ideal for the initial challenge submission and local testing.
  - **SQLAlchemy 2.0**: The industry standard for Python database interactions. It provides a robust Data Mapper pattern, excellent typing support, and seamless transitioning across SQL dialects.
* **Cons**: SQLite does not handle high concurrent writes well; SQLAlchemy has a steeper learning curve compared to lightweight ORMs.
* **Final choice**: SQLite + SQLAlchemy 2.0.
* **Reasoning**: We prioritize "production-readiness". While SQLite is our initial choice for simplicity, using SQLAlchemy guarantees that our business logic and data access layers are decoupled from the underlying database dialect. If the Store Intelligence System needs to scale to thousands of concurrent camera events, we only need to change the connection string and migration scripts to switch to PostgreSQL. Setting `expire_on_commit=False` allows us to cleanly separate DB transactions from Pydantic model serialization later in the request lifecycle without requiring explicit eager loading or detached instance handling.
