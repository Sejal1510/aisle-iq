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
