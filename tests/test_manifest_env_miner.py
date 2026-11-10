"""Env/config mining: pure parsers that turn dotenv, compose and helm env into dependency facts."""

from __future__ import annotations

import pytest

from bifrost.manifest.env_miner import (
    MinedDeps,
    iter_compose_env,
    iter_helm_env,
    mine_file,
    mine_mapping,
    parse_dotenv,
    scrub_value,
)


class TestParseDotenv:
    def test_plain_pairs(self) -> None:
        assert parse_dotenv("A=1\nB=two\n") == {"A": "1", "B": "two"}

    def test_comments_and_blank_lines_ignored(self) -> None:
        assert parse_dotenv("# header\n\nA=1\n   # indented comment\n") == {"A": "1"}

    def test_export_prefix(self) -> None:
        assert parse_dotenv("export A=1\nexport   B=2") == {"A": "1", "B": "2"}

    def test_quotes_stripped(self) -> None:
        assert parse_dotenv("A=\"x y\"\nB='z'\n") == {"A": "x y", "B": "z"}

    def test_hash_inside_quotes_is_kept(self) -> None:
        assert parse_dotenv('A="x # not a comment"\n') == {"A": "x # not a comment"}

    def test_inline_comment_after_unquoted_value(self) -> None:
        assert parse_dotenv("A=value # trailing\n") == {"A": "value"}

    def test_hash_without_leading_space_is_part_of_value(self) -> None:
        assert parse_dotenv("A=abc#def\n") == {"A": "abc#def"}

    def test_empty_value(self) -> None:
        assert parse_dotenv("A=\n") == {"A": ""}

    def test_value_containing_equals(self) -> None:
        assert parse_dotenv("A=x=y=z\n") == {"A": "x=y=z"}

    def test_spaces_around_equals(self) -> None:
        assert parse_dotenv("A = 1\n") == {"A": "1"}

    def test_later_duplicate_wins(self) -> None:
        assert parse_dotenv("A=1\nA=2\n") == {"A": "2"}

    def test_line_without_equals_ignored(self) -> None:
        assert parse_dotenv("JUSTAWORD\nA=1\n") == {"A": "1"}

    def test_crlf_line_endings(self) -> None:
        assert parse_dotenv("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}


class TestMineMapping:
    def test_host_key_becomes_downstream_service(self) -> None:
        into = MinedDeps()
        mine_mapping({"DELIVERY_GATEWAY_HOST": "delivery-gateway.us.acme.internal"}, into)
        assert into.downstream_services == {"delivery_gateway": "delivery-gateway.us.acme.internal"}

    def test_url_key_with_https_value(self) -> None:
        into = MinedDeps()
        mine_mapping({"INVENTORY_URL": "https://inventory.example.com/v1"}, into)
        assert into.downstream_services == {"inventory": "https://inventory.example.com/v1"}

    def test_first_value_wins_for_same_service(self) -> None:
        into = MinedDeps()
        mine_mapping({"SVC_HOST": "a.example.com"}, into)
        mine_mapping({"SVC_HOST": "b.example.com"}, into)
        assert into.downstream_services == {"svc": "a.example.com"}

    def test_non_host_value_for_host_key_is_ignored(self) -> None:
        into = MinedDeps()
        mine_mapping({"SOME_HOST": "not a host"}, into)
        assert into.downstream_services == {}

    def test_queue_and_topic_keys(self) -> None:
        into = MinedDeps()
        mine_mapping(
            {"DELIVERY_QUEUE": "delivery-events-us", "BILLING_TOPIC": "billing-usage-us"}, into
        )
        assert into.messaging_topics == {
            "delivery": "delivery-events-us",
            "billing": "billing-usage-us",
        }

    def test_queue_url_goes_to_topics_not_services(self) -> None:
        into = MinedDeps()
        mine_mapping({"JOBS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/jobs"}, into)
        assert "jobs" in into.messaging_topics
        assert into.downstream_services == {}

    def test_topic_values_collect_every_environment(self) -> None:
        into = MinedDeps()
        mine_mapping({"DELIVERY_QUEUE": "delivery-events-us"}, into)
        mine_mapping({"DELIVERY_QUEUE": "delivery-events-eu"}, into)
        assert into.topic_values == {"delivery": {"delivery-events-us", "delivery-events-eu"}}
        assert into.messaging_topics == {"delivery": "delivery-events-us"}

    @pytest.mark.parametrize(
        "key",
        ["API_BASE_URL", "PUBLIC_URL", "SECRET_HOST", "CLIENT_URL", "PRIVATE_HOST", "OAUTH_URL"],
    )
    def test_negative_prefixes_are_not_services(self, key: str) -> None:
        into = MinedDeps()
        mine_mapping({key: "https://x.example.com"}, into)
        assert into.downstream_services == {}

    @pytest.mark.parametrize(
        "key", ["MAX_BATCH", "REGION", "FLUSH_INTERVAL_SECONDS", "MAX_ATTEMPTS"]
    )
    def test_non_dependency_keys_ignored(self, key: str) -> None:
        into = MinedDeps()
        mine_mapping({key: "50"}, into)
        assert into == MinedDeps()

    def test_non_scalar_and_non_string_keys_skipped(self) -> None:
        into = MinedDeps()
        mine_mapping({"A_HOST": {"nested": "x"}, 5: "y", "B_HOST": ["z"]}, into)  # type: ignore[dict-item]
        assert into == MinedDeps()

    def test_jdbc_database_name(self) -> None:
        into = MinedDeps()
        mine_mapping({"DB": "jdbc:postgresql://db.example.com:5432/Orders?ssl=true"}, into)
        assert into.database_names == ["orders"]

    @pytest.mark.parametrize(
        ("url", "db"),
        [
            ("postgresql://u@h:5432/subscriptions", "subscriptions"),
            ("postgres://u:p@h/inventory", "inventory"),
            ("mysql://u:p@h/shop", "shop"),
            ("mariadb://h/inventory", "inventory"),
            ("mongodb://h1:27017,h2:27017/events?replicaSet=rs0", "events"),
            ("mongodb+srv://u:p@cluster.example.net/audit", "audit"),
            ("sqlserver://h:1433/orders", "orders"),
            ("redis://h:6379/sessions", "sessions"),
        ],
    )
    def test_database_urls(self, url: str, db: str) -> None:
        into = MinedDeps()
        mine_mapping({"SOME_DB_URL": url}, into)
        assert into.database_names == [db]

    def test_redis_numeric_database_index_is_not_a_name(self) -> None:
        into = MinedDeps()
        mine_mapping({"CACHE": "redis://h:6379/0"}, into)
        assert into.database_names == []

    def test_database_names_deduplicated(self) -> None:
        into = MinedDeps()
        mine_mapping({"A": "postgresql://h/subs", "B": "postgresql://other/subs"}, into)
        assert into.database_names == ["subs"]

    def test_userinfo_never_leaks(self) -> None:
        into = MinedDeps()
        mine_mapping(
            {
                "SUBS_DB_URL": "postgresql://admin:hunter2@subs.example.com/subscriptions",
                "INVENTORY_URL": "https://bob:hunter2@inventory.example.com/v1",
            },
            into,
        )
        assert into.database_names == ["subscriptions"]
        assert "hunter2" not in repr(into)
        assert "admin" not in repr(into)
        assert into.downstream_services == {"inventory": "https://inventory.example.com/v1"}


class TestScrubValue:
    def test_strips_userinfo(self) -> None:
        assert scrub_value("https://u:p@host.example.com/x") == "https://host.example.com/x"

    def test_user_only(self) -> None:
        assert scrub_value("postgresql://dispatcher@subs/db") == "postgresql://subs/db"

    def test_leaves_plain_values(self) -> None:
        assert scrub_value("delivery-events-us") == "delivery-events-us"
        assert scrub_value("https://host/a@b") == "https://host/a@b"


class TestComposeEnv:
    def test_list_form(self) -> None:
        text = """
services:
  app:
    environment:
      - INVENTORY_HOST=inventory.example.com
      - PLAIN
      - 'QUOTED_QUEUE=jobs'
"""
        assert dict(iter_compose_env(text)) == {
            "INVENTORY_HOST": "inventory.example.com",
            "QUOTED_QUEUE": "jobs",
        }

    def test_dict_form(self) -> None:
        text = """
services:
  app:
    environment:
      INVENTORY_HOST: inventory.example.com
      MAX_BATCH: 50
  worker:
    environment:
      JOBS_QUEUE: jobs
"""
        assert dict(iter_compose_env(text)) == {
            "INVENTORY_HOST": "inventory.example.com",
            "MAX_BATCH": "50",
            "JOBS_QUEUE": "jobs",
        }

    def test_malformed_or_unrelated_yaml_yields_nothing(self) -> None:
        assert list(iter_compose_env("a: [unclosed")) == []
        assert list(iter_compose_env("just: a mapping\n")) == []


class TestHelmEnv:
    def test_env_list_of_name_value(self) -> None:
        text = """
env:
  - name: INVENTORY_HOST
    value: inventory.example.com
  - name: FROM_SECRET
    valueFrom:
      secretKeyRef: {name: s, key: k}
"""
        assert dict(iter_helm_env(text)) == {"INVENTORY_HOST": "inventory.example.com"}

    def test_env_mapping(self) -> None:
        text = """
app:
  env:
    JOBS_QUEUE: jobs
"""
        assert dict(iter_helm_env(text)) == {"JOBS_QUEUE": "jobs"}

    def test_malformed_yaml_yields_nothing(self) -> None:
        assert list(iter_helm_env("env: [unclosed")) == []


class TestMineFile:
    def test_dispatches_dotenv_by_name(self) -> None:
        into = MinedDeps()
        mine_file("config/us.env", "DELIVERY_QUEUE=q-us\nSVC_HOST=svc.example.com\n", into)
        assert into.messaging_topics == {"delivery": "q-us"}
        assert into.downstream_services == {"svc": "svc.example.com"}

    def test_dotenv_example_name(self) -> None:
        into = MinedDeps()
        mine_file(".env.example", "SVC_HOST=svc.example.com\n", into)
        assert into.downstream_services == {"svc": "svc.example.com"}

    def test_dispatches_compose(self) -> None:
        into = MinedDeps()
        mine_file(
            "docker-compose.yml",
            "services:\n  a:\n    environment:\n      - SVC_HOST=svc.example.com\n",
            into,
        )
        assert into.downstream_services == {"svc": "svc.example.com"}

    def test_dispatches_helm_values(self) -> None:
        into = MinedDeps()
        mine_file("helm/app/values-prod.yaml", "env:\n  JOBS_QUEUE: jobs\n", into)
        assert into.messaging_topics == {"jobs": "jobs"}

    def test_unknown_file_type_is_ignored(self) -> None:
        into = MinedDeps()
        mine_file("README.md", "SVC_HOST=svc.example.com\n", into)
        assert into == MinedDeps()

    def test_stores_only_matches_not_whole_file(self) -> None:
        into = MinedDeps()
        mine_file("config/us.env", "PASSWORD=hunter2\nSVC_HOST=svc.example.com\n", into)
        assert "hunter2" not in repr(into)
