from credit.models import User


class AccessDenied(ValueError):
    pass


ROLE_TOOLS = {
    "analyst": {"catalog", "definition", "field", "lineage", "schema", "query", "calculation"},
    "operator": {"catalog", "definition", "field", "lineage", "schema", "query", "calculation"},
    "visitor": {"definition", "calculation"},
}


def authorize(user: User, tool: str, roles: list[str], domain: str = "credit"):
    permitted = set().union(*(ROLE_TOOLS.get(role, set()) for role in user.roles))
    if tool not in permitted or not user.roles.intersection(roles) or domain not in user.domains:
        raise AccessDenied("当前身份无权访问该工具或数据域")


# No request-body role, header role, or memory field can alter this mapping.
DEMO_USERS = {
    "demo-analyst": User("analyst-001", frozenset({"analyst"}), frozenset({"credit"})),
    "demo-operator": User("operator-001", frozenset({"operator"}), frozenset({"credit"})),
    "demo-visitor": User("visitor-001", frozenset({"visitor"}), frozenset({"credit"})),
    # Platform observability has no data-domain or query-tool permissions.
    "demo-platform": User("platform-001", frozenset({"platform"}), frozenset()),
}
