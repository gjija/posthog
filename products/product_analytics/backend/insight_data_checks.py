import re
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from posthog.schema import (
    DateRange,
    EventsNode,
    FunnelsQuery,
    HogQLFilters,
    LifecycleQuery,
    StickinessQuery,
    TrendsQuery,
)

from posthog.hogql import ast
from posthog.hogql.constants import HogQLGlobalSettings
from posthog.hogql.property import property_to_expr
from posthog.hogql.query import execute_hogql_query

from posthog.clickhouse.query_tagging import Feature, Product, tags_context
from posthog.dataclasses import frozen
from posthog.hogql_queries.apply_dashboard_filters import WRAPPER_NODE_KINDS
from posthog.models.organization import OrganizationMembership
from posthog.models.organization_domain import OrganizationDomain
from posthog.models.team import Team
from posthog.utils import GenericEmails

from products.product_analytics.backend.facade.contracts import InsightDataCheck, InsightDataChecks
from products.product_analytics.backend.facade.enums import (
    InsightDataCheckAction,
    InsightDataCheckKey,
    InsightDataCheckLevel,
    InsightDataChecksStatus,
)

# Each check is a share of the events, so a sample this large gives the same answer as every event.
MAX_CHECKED_EVENTS = 1_000_000
MAX_EXECUTION_SECONDS = 20
DEFAULT_DATE_RANGE = DateRange(date_from="-7d")

LOCAL_HOST_PATTERN = r"(?i)^(localhost|[a-z0-9.-]+\.localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:[0-9]+)?$"
# An event with no user agent has the type "Automation", and server-side SDKs send those, so it is not a crawler.
CRAWLER_TRAFFIC_TYPES = ("Bot", "AI Agent")

# A real person's email can contain one of these words, so only a word that stands alone marks a test account.
TEST_ACCOUNT_WORDS = "test|testing|qa|demo|dummy|fake"
# A member on a company domain is found by the domain, so these limits only bound the size of the query.
MAX_OWN_TEAM_DOMAINS = 20
MAX_OWN_TEAM_EMAILS = 200
# Whether a person is internal comes from the person's properties. They are stored together, which makes them the
# most expensive value these checks read, so the internal users check reads this many events at most.
MAX_INTERNAL_USERS_CHECKED_EVENTS = 50_000
# A few busy people can fill a small sample, so this check needs a larger share than the others to report a finding.
MIN_INTERNAL_USERS_SHARE = 0.02

MIN_FINDING_EVENTS = 50
MIN_FINDING_SHARE = 0.01
MIN_DUPLICATE_EVENTS = 100
MIN_DUPLICATE_SHARE = 0.05
PROBLEM_SHARE = 0.2

_WRAPPER_KINDS = {kind.value for kind in WRAPPER_NODE_KINDS}
_SERIES_QUERIES: dict[str, type[TrendsQuery | FunnelsQuery | StickinessQuery | LifecycleQuery]] = {
    "TrendsQuery": TrendsQuery,
    "FunnelsQuery": FunnelsQuery,
    "StickinessQuery": StickinessQuery,
    "LifecycleQuery": LifecycleQuery,
}

# Event names and dates are in the sort key of the events table, so this count reads no property of any event.
_TOTAL_QUERY = "SELECT count() FROM events WHERE {filters} AND {event_condition}"

# The traffic type runs once for each user agent and not for each event, which keeps a chart of every event cheap.
_COUNTS_QUERY = """
    SELECT
        sum(event_count) AS events,
        sum(local_count) AS local_events,
        sumIf(event_count, getTrafficType(user_agent) IN {crawler_traffic_types}) AS bot_events,
        sum(distinct_count) AS distinct_events
    FROM (
        SELECT
            toString(properties.$raw_user_agent) AS user_agent,
            count() AS event_count,
            countIf(match(toString(properties.$host), {local_host_pattern})) AS local_count,
            uniq((
                distinct_id,
                event,
                toStartOfSecond(timestamp),
                toString(properties.$current_url),
                toString(properties.$screen_name),
                toString(properties.$feature_flag),
                elements_chain
            )) AS distinct_count
        FROM events
        SAMPLE {sample}
        WHERE {filters}
            AND {insight_filters}
            AND {event_condition}
        GROUP BY user_agent
    )
"""

_INTERNAL_USERS_QUERY = """
    SELECT
        count() AS events,
        countIf(NOT ifNull({kept_by_filter}, false)) AS removed_by_filter,
        countIf(
            ifNull({kept_by_filter}, false)
            AND (
                match(lower(toString(person.properties.email)), {own_team_pattern})
                OR match(lower(distinct_id), {own_team_pattern})
            )
        ) AS missed_by_filter
    FROM events
    SAMPLE {sample}
    WHERE {filters}
        AND {insight_filters}
        AND {event_condition}
"""


@frozen
class InternalUserCounts:
    # The internal users check reads its own, smaller sample of the insight's events.
    events: int
    removed_by_filter: int
    missed_by_filter: int
    is_sample: bool = False

    @property
    def internal_events(self) -> int:
        return self.removed_by_filter + self.missed_by_filter


@frozen
class DataCheckCounts:
    events: int
    local_events: int
    bot_events: int
    distinct_events: int
    internal_users: InternalUserCounts | None = None
    sample_rate: float = 1.0


@frozen
class OwnTeamEmails:
    # Company domains, the one with the most members first.
    domains: tuple[str, ...]
    # Members on a public email provider, where the domain says nothing about the team.
    addresses: tuple[str, ...]


@frozen
class _CheckedQuery:
    event_names: tuple[str, ...] | None
    date_range: DateRange
    properties: Any
    filters_internal_users: bool


def run_insight_data_checks(query: Mapping[str, Any], team: Team) -> InsightDataChecks:
    """Check the events an insight reads for the common ways they mislead.

    Runs three ClickHouse queries: a cheap count that sets the samples, the checks on a large sample, and the
    internal users check on a small one.
    """
    checked = _checked_query(query)
    if checked is None:
        return _not_checked(InsightDataChecksStatus.UNSUPPORTED)
    has_internal_users_filter = bool(team.test_account_filters)
    own_team = _own_team_emails(team)
    with tags_context(product=Product.PRODUCT_ANALYTICS, feature=Feature.INSIGHT, name="insight_data_checks"):
        total = _count_events_in_range(checked, team)
        if total == 0:
            return _not_checked(InsightDataChecksStatus.NO_EVENTS)
        counts = _count_events(
            checked, team, total=total, has_internal_users_filter=has_internal_users_filter, own_team=own_team
        )
    # The insight's own filters can remove every event the sample holds.
    if counts.events == 0:
        return _not_checked(InsightDataChecksStatus.NO_EVENTS)
    checks = evaluate_data_checks(
        counts,
        filters_internal_users=checked.filters_internal_users,
        has_internal_users_filter=has_internal_users_filter,
        own_team_domain=own_team.domains[0] if own_team.domains else None,
    )
    return InsightDataChecks(
        status=_status_of(checks), checks=checks, events_checked=counts.events, sample_rate=counts.sample_rate
    )


def evaluate_data_checks(
    counts: DataCheckCounts,
    *,
    filters_internal_users: bool,
    has_internal_users_filter: bool,
    own_team_domain: str | None = None,
) -> list[InsightDataCheck]:
    """Every check, in a fixed order, so the list shows what was checked whatever the result."""
    return [
        _internal_users_check(
            counts,
            filters_internal_users=filters_internal_users,
            has_internal_users_filter=has_internal_users_filter,
            own_team_domain=own_team_domain,
        ),
        _local_development_check(counts),
        _bot_traffic_check(counts),
        _duplicate_events_check(counts),
    ]


def _checked_query(query: Mapping[str, Any]) -> _CheckedQuery | None:
    node: Any = query
    while isinstance(node, Mapping) and node.get("kind") in _WRAPPER_KINDS:
        node = node.get("source")
    if not isinstance(node, Mapping):
        return None
    model = _SERIES_QUERIES.get(str(node.get("kind")))
    if model is None:
        return None
    try:
        source = model.model_validate(node)
    except ValidationError:
        return None
    if not source.series:
        return None

    names: list[str] = []
    reads_every_event = False
    for series in source.series:
        # An action or a data warehouse series has no event name to check.
        if not isinstance(series, EventsNode):
            return None
        if series.event:
            names.append(series.event)
        else:
            reads_every_event = True
    return _CheckedQuery(
        event_names=None if reads_every_event else tuple(dict.fromkeys(names)),
        date_range=source.dateRange or DEFAULT_DATE_RANGE,
        properties=source.properties,
        filters_internal_users=bool(source.filterTestAccounts),
    )


@lru_cache(maxsize=1)
def _generic_emails() -> GenericEmails:
    return GenericEmails()


def _own_team_emails(team: Team) -> OwnTeamEmails:
    """The emails of the people who work on this project: the members of its organization, and its verified domains."""
    member_emails = OrganizationMembership.objects.filter(organization_id=team.organization_id).values_list(
        "user__email", flat=True
    )
    domains: Counter[str] = Counter()
    addresses: set[str] = set()
    for member_email in member_emails:
        email = member_email.strip().lower()
        if "@" not in email:
            continue
        if _generic_emails().is_generic(email):
            addresses.add(email)
        else:
            domains[email.rpartition("@")[2]] += 1
    verified_domains = OrganizationDomain.objects.verified_domains().filter(organization_id=team.organization_id)
    for domain in verified_domains.values_list("domain", flat=True):
        domains.setdefault(domain.strip().lower(), 0)
    return OwnTeamEmails(
        domains=tuple(domain for domain, _ in sorted(domains.items(), key=lambda item: (-item[1], item[0])))[
            :MAX_OWN_TEAM_DOMAINS
        ],
        addresses=tuple(sorted(addresses))[:MAX_OWN_TEAM_EMAILS],
    )


def own_team_email_pattern(domains: Sequence[str], addresses: Sequence[str]) -> str:
    """A pattern for a lowercase email that belongs to the project's own team or to a test account."""
    alternatives = [
        rf"^(?:{TEST_ACCOUNT_WORDS})(?:[._+0-9-][^@]*)?@",
        rf"\+(?:{TEST_ACCOUNT_WORDS})(?:[._0-9-][^@]*)?@",
    ]
    if domains:
        # A subdomain of a company domain is the same company. The end anchor keeps acme.com.br out.
        alternatives.append(rf"@(?:[a-z0-9-]+\.)*(?:{'|'.join(re.escape(domain) for domain in domains)})$")
    if addresses:
        alternatives.append(rf"^(?:{'|'.join(re.escape(address) for address in addresses)})$")
    return "|".join(f"(?:{alternative})" for alternative in alternatives)


def _event_condition(checked: _CheckedQuery) -> ast.Expr:
    if checked.event_names is None:
        return ast.Constant(value=True)
    return ast.CompareOperation(
        op=ast.CompareOperationOp.In,
        left=ast.Field(chain=["event"]),
        right=ast.Tuple(exprs=[ast.Constant(value=name) for name in checked.event_names]),
    )


def _count_events_in_range(checked: _CheckedQuery, team: Team) -> int:
    """Every event with the insight's event names in its date range, before any property filter."""
    response = execute_hogql_query(
        query=_TOTAL_QUERY,
        team=team,
        query_type="insight_data_checks_total",
        filters=HogQLFilters(dateRange=checked.date_range),
        placeholders={"event_condition": _event_condition(checked)},
        settings=HogQLGlobalSettings(max_execution_time=MAX_EXECUTION_SECONDS),
    )
    return int(response.results[0][0] or 0) if response.results else 0


def _count_events(
    checked: _CheckedQuery, team: Team, *, total: int, has_internal_users_filter: bool, own_team: OwnTeamEmails
) -> DataCheckCounts:
    is_sampled = total > MAX_CHECKED_EVENTS
    insight_filters = property_to_expr(checked.properties, team) if checked.properties else ast.Constant(value=True)
    response = execute_hogql_query(
        query=_COUNTS_QUERY,
        team=team,
        query_type="insight_data_checks",
        filters=HogQLFilters(dateRange=checked.date_range, filterTestAccounts=checked.filters_internal_users),
        placeholders={
            "crawler_traffic_types": ast.Tuple(exprs=[ast.Constant(value=name) for name in CRAWLER_TRAFFIC_TYPES]),
            "local_host_pattern": ast.Constant(value=LOCAL_HOST_PATTERN),
            # The events table samples by distinct ID, so a sampled ID keeps all its events and no duplicate pair
            # is split.
            "sample": _sample(MAX_CHECKED_EVENTS, total),
            "insight_filters": insight_filters,
            "event_condition": _event_condition(checked),
        },
        settings=HogQLGlobalSettings(max_execution_time=MAX_EXECUTION_SECONDS),
    )
    row = response.results[0] if response.results else []
    events, local_events, bot_events, distinct_events = (int(value or 0) for value in row or [0] * 4)
    return DataCheckCounts(
        events=events,
        local_events=local_events,
        bot_events=bot_events,
        distinct_events=distinct_events,
        internal_users=_count_internal_user_events(
            checked,
            team,
            total=total,
            has_internal_users_filter=has_internal_users_filter,
            own_team=own_team,
            insight_filters=insight_filters,
        )
        if events
        else None,
        sample_rate=MAX_CHECKED_EVENTS / total if is_sampled else 1.0,
    )


def _sample(limit: int, total: int) -> ast.RatioExpr:
    if total > limit:
        return ast.RatioExpr(left=ast.Constant(value=limit), right=ast.Constant(value=total))
    return ast.RatioExpr(left=ast.Constant(value=1))


def _count_internal_user_events(
    checked: _CheckedQuery,
    team: Team,
    *,
    total: int,
    has_internal_users_filter: bool,
    own_team: OwnTeamEmails,
    insight_filters: ast.Expr,
) -> InternalUserCounts:
    # When the insight applies the filter, the events it would remove are gone before this count.
    counts_filtered_events = has_internal_users_filter and not checked.filters_internal_users
    response = execute_hogql_query(
        query=_INTERNAL_USERS_QUERY,
        team=team,
        query_type="insight_data_checks_internal_users",
        filters=HogQLFilters(dateRange=checked.date_range, filterTestAccounts=checked.filters_internal_users),
        placeholders={
            "sample": _sample(MAX_INTERNAL_USERS_CHECKED_EVENTS, total),
            "insight_filters": insight_filters,
            "event_condition": _event_condition(checked),
            "kept_by_filter": (
                property_to_expr(team.test_account_filters, team)
                if counts_filtered_events
                else ast.Constant(value=True)
            ),
            "own_team_pattern": ast.Constant(value=own_team_email_pattern(own_team.domains, own_team.addresses)),
        },
        settings=HogQLGlobalSettings(max_execution_time=MAX_EXECUTION_SECONDS),
    )
    row = response.results[0] if response.results else []
    events, removed_by_filter, missed_by_filter = (int(value or 0) for value in row or [0] * 3)
    return InternalUserCounts(
        events=events,
        removed_by_filter=removed_by_filter,
        missed_by_filter=missed_by_filter,
        is_sample=total > MAX_INTERNAL_USERS_CHECKED_EVENTS,
    )


def _check(
    key: InsightDataCheckKey,
    level: InsightDataCheckLevel,
    result: str,
    *,
    fix: str | None = None,
    action: InsightDataCheckAction | None = None,
    events: int | None = None,
    share: float | None = None,
) -> InsightDataCheck:
    return InsightDataCheck(
        key=key, level=level, name=key.label, result=result, fix=fix, action=action, events=events, share=share
    )


def _not_checked(status: InsightDataChecksStatus) -> InsightDataChecks:
    checks = [_check(key, InsightDataCheckLevel.SKIPPED, key.label) for key in InsightDataCheckKey]
    return InsightDataChecks(status=status, checks=checks, events_checked=0, sample_rate=1.0)


def _status_of(checks: list[InsightDataCheck]) -> InsightDataChecksStatus:
    levels = {check.level for check in checks}
    if InsightDataCheckLevel.PROBLEM in levels:
        return InsightDataChecksStatus.PROBLEM
    return (
        InsightDataChecksStatus.WARNING if InsightDataCheckLevel.WARNING in levels else InsightDataChecksStatus.PASSED
    )


def _finding_level(share: float) -> InsightDataCheckLevel:
    return InsightDataCheckLevel.PROBLEM if share >= PROBLEM_SHARE else InsightDataCheckLevel.WARNING


def _is_finding(count: int, share: float) -> bool:
    return count >= MIN_FINDING_EVENTS and share >= MIN_FINDING_SHARE


def _percent(share: float) -> str:
    return f"{round(share * 100)}%"


def _event_count(count: int, counts: DataCheckCounts) -> str:
    """A count of the insight's events. From a sample it is an estimate, so it is scaled up and rounded."""
    if counts.sample_rate >= 1:
        return f"{count:,}"
    estimate = round(count / counts.sample_rate)
    magnitude = 10 ** max(len(str(estimate)) - 2, 0)
    return f"about {round(estimate / magnitude) * magnitude:,}"


def _passed(key: InsightDataCheckKey, things: str, count: int, counts: DataCheckCounts) -> InsightDataCheck:
    """`things` is what the check looks for, as it reads after "No": "bots or crawlers"."""
    result = f"Almost no {things} found ({_event_count(count, counts)} events)" if count else f"No {things} found"
    return _check(key, InsightDataCheckLevel.PASSED, result, events=count, share=count / counts.events)


def _is_internal_users_finding(count: int, internal_users: InternalUserCounts) -> bool:
    return count >= MIN_FINDING_EVENTS and count >= MIN_INTERNAL_USERS_SHARE * internal_users.events


def _internal_users_check(
    counts: DataCheckCounts,
    *,
    filters_internal_users: bool,
    has_internal_users_filter: bool,
    own_team_domain: str | None,
) -> InsightDataCheck:
    key = InsightDataCheckKey.INTERNAL_USERS
    internal_users = counts.internal_users
    if internal_users is None or internal_users.events == 0:
        internal_users = InternalUserCounts(events=1, removed_by_filter=0, missed_by_filter=0)
    share = internal_users.internal_events / internal_users.events
    # The other checks count the larger sample, so this count is given on the same scale.
    events = round(share * counts.events)
    if _is_internal_users_finding(internal_users.internal_events, internal_users):
        example = f", such as emails that end in @{own_team_domain}" if own_team_domain else ""
        action = InsightDataCheckAction.OPEN_FILTER_SETTINGS
        if not has_internal_users_filter:
            fix = f"Set up the internal and test users filter so that it removes your team's emails{example}."
        elif _is_internal_users_finding(internal_users.missed_by_filter, internal_users):
            fix = (
                "The internal and test users filter does not remove all of them. "
                f"Change the filter so that it also removes your team's emails{example}."
            )
        else:
            fix = "Turn on the internal and test users filter for this insight."
            action = InsightDataCheckAction.FILTER_INTERNAL_USERS
        if action == InsightDataCheckAction.OPEN_FILTER_SETTINGS and not filters_internal_users:
            fix += " Then turn the filter on for this insight."
        # A sample of people can hold more or fewer internal users than the whole insight does.
        amount = f"About {_percent(share)}" if internal_users.is_sample else _percent(share)
        return _check(
            key,
            _finding_level(share),
            f"{amount} of these events came from internal and test users",
            fix=fix,
            action=action,
            events=events,
            share=share,
        )
    if filters_internal_users:
        return _check(key, InsightDataCheckLevel.PASSED, "Internal and test users are filtered out")
    if not has_internal_users_filter:
        return _check(
            key,
            InsightDataCheckLevel.SKIPPED,
            "This project has no filter for internal and test users",
            fix="Set up the filter so PostHog knows which users are your own team.",
            action=InsightDataCheckAction.OPEN_FILTER_SETTINGS,
        )
    return _passed(key, "internal or test users", events, counts)


def _local_development_check(counts: DataCheckCounts) -> InsightDataCheck:
    key = InsightDataCheckKey.LOCAL_DEVELOPMENT
    share = counts.local_events / counts.events
    # No minimum share: a local session also creates test persons and flag exposures, whatever its share.
    if counts.local_events < MIN_FINDING_EVENTS:
        return _passed(key, "local development events", counts.local_events, counts)
    amount = _percent(share) if share >= MIN_FINDING_SHARE else _event_count(counts.local_events, counts)
    return _check(
        key,
        _finding_level(share),
        f"{amount[0].upper()}{amount[1:]} of these events came from local development",
        fix=(
            "Send local traffic to a separate project, or exclude localhost in the internal and test users filter "
            "and turn that filter on for this insight."
        ),
        action=InsightDataCheckAction.OPEN_FILTER_SETTINGS,
        events=counts.local_events,
        share=share,
    )


def _bot_traffic_check(counts: DataCheckCounts) -> InsightDataCheck:
    key = InsightDataCheckKey.BOT_TRAFFIC
    share = counts.bot_events / counts.events
    if not _is_finding(counts.bot_events, share):
        return _passed(key, "bots or crawlers", counts.bot_events, counts)
    return _check(
        key,
        _finding_level(share),
        f"{_percent(share)} of these events came from bots and crawlers",
        fix=(
            'In the internal and test users filter, add "Traffic type is not Bot or AI Agent", '
            "and turn that filter on for this insight."
        ),
        action=InsightDataCheckAction.OPEN_FILTER_SETTINGS,
        events=counts.bot_events,
        share=share,
    )


def _duplicate_events_check(counts: DataCheckCounts) -> InsightDataCheck:
    key = InsightDataCheckKey.DUPLICATE_EVENTS
    duplicates = max(counts.events - counts.distinct_events, 0)
    share = duplicates / counts.events
    if duplicates < MIN_DUPLICATE_EVENTS or share < MIN_DUPLICATE_SHARE:
        # The count is an estimate, so a small one can be an error of the estimate and not a real event.
        result = (
            "No events sent twice" if share < MIN_FINDING_SHARE else f"Few events sent twice (about {_percent(share)})"
        )
        return _check(key, InsightDataCheckLevel.PASSED, result, events=duplicates, share=share)
    return _check(
        key,
        _finding_level(share),
        f"About {_percent(share)} of these events were sent twice within one second",
        fix="Start PostHog once in your app, and send each event from one place only.",
        events=duplicates,
        share=share,
    )
