"""Closed vocabularies used by versioned API and persistence contracts."""

from enum import StrEnum


class CoverageStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    BLOCKED = "blocked"
    PARTIAL = "partial"


class FetchStatus(StrEnum):
    SUCCESS = "success"
    NOT_MODIFIED = "not_modified"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProductFamily(StrEnum):
    FINANCING = "financing"
    CARD = "card"
    INVESTMENT = "investment"
    PARTICIPATION_ACCOUNT = "participation_account"
    OTHER = "other"
    UNKNOWN = "unknown"


class CampaignType(StrEnum):
    FINANCING_RATE = "financing_rate"
    PROFIT_SHARE = "profit_share"
    INSTALLMENT = "installment"
    CASHBACK = "cashback"
    DISCOUNT = "discount"
    POINTS = "points"
    FEE_WAIVER = "fee_waiver"
    WELCOME = "welcome"
    OTHER = "other"
    UNKNOWN = "unknown"


class RateKind(StrEnum):
    FINANCING_PROFIT_RATE = "financing_profit_rate"
    ANNUAL_COST_RATE = "annual_cost_rate"
    PROFIT_SHARE_DISTRIBUTION_RATE = "profit_share_distribution_rate"
    HISTORICAL_RETURN_RATE = "historical_return_rate"
    DISCOUNT_RATE = "discount_rate"
    REWARD_RATE = "reward_rate"
    UNKNOWN = "unknown"


class RatePeriod(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"
    TERM_TOTAL = "term_total"
    UNSPECIFIED = "unspecified"


class RewardKind(StrEnum):
    MONEY = "money"
    POINTS = "points"
    DISCOUNT = "discount"
    FREE_SERVICE = "free_service"
    OTHER = "other"


class RewardBasis(StrEnum):
    CAMPAIGN_TOTAL = "campaign_total"
    PER_CUSTOMER = "per_customer"
    PER_TRANSACTION = "per_transaction"
    PER_MONTH = "per_month"
    UNSPECIFIED = "unspecified"


class FeeKind(StrEnum):
    ALLOCATION = "allocation"
    ANNUAL = "annual"
    MONTHLY = "monthly"
    TRANSACTION = "transaction"
    EARLY_EXIT = "early_exit"
    OTHER = "other"
    UNKNOWN = "unknown"


class FeeBasis(StrEnum):
    ONE_TIME = "one_time"
    PER_MONTH = "per_month"
    PER_YEAR = "per_year"
    PER_TRANSACTION = "per_transaction"
    PERCENT_OF_AMOUNT = "percent_of_amount"
    UNSPECIFIED = "unspecified"


class GrossNetBasis(StrEnum):
    GROSS = "gross"
    NET = "net"
    UNSPECIFIED = "unspecified"


class SalesChannel(StrEnum):
    ALL = "all"
    DIGITAL = "digital"
    MOBILE = "mobile"
    WEB = "web"
    BRANCH = "branch"
    UNKNOWN = "unknown"


class EvidenceStatus(StrEnum):
    STATED = "stated"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


class ExtractionMethod(StrEnum):
    RULE = "rule"
    LLM = "llm"
    HYBRID = "hybrid"
    MANUAL = "manual"


class RecordStatus(StrEnum):
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class QueryIntent(StrEnum):
    LIST = "list"
    DETAIL = "detail"
    COMPARE = "compare"
    COVERAGE = "coverage"
    GLOSSARY = "glossary"
    UNKNOWN = "unknown"


class ComparisonDimension(StrEnum):
    RATE = "rate"
    TERM = "term"
    FEE = "fee"
    REWARD = "reward"
    ELIGIBILITY = "eligibility"


class CitationStatus(StrEnum):
    VERIFIED = "verified"
    MISSING = "missing"
    MISMATCH = "mismatch"
