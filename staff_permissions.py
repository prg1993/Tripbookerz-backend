"""
Role -> permission map for the staff portal.

Source of truth for WHAT each role may do is the owner-approved sheet
Tripbookerz_Staff_Portal_Access_Design.xlsx (tab "Roles & Permissions");
this file transcribes it. Routes check PERMISSIONS, never role names, so
re-mapping a role is a change to this one table.

Every person holds "staff" in addition to any functional role.
"""

ALL_PERMISSIONS = [
    "staff.login",
    "sales.view_own", "sales.view_team", "sales.view_periods",
    "target.set",
    "coupon.view", "coupon.create", "coupon.disable", "coupon.approve_large",
    "campaign.manage",
    "expense.create", "expense.view", "finance.view_pnl", "finance.export",
    "booking.view_detail", "markup.manage", "gst.manage", "privacy.manage",
    "user.create", "user.disable", "user.assign_role", "user.reset_password",
    "audit.view",
]

ROLE_PERMISSIONS = {
    "staff": {"staff.login"},
    "sales": {"staff.login", "sales.view_own", "sales.view_team", "sales.view_periods"},
    "sales_manager": {
        "staff.login", "sales.view_own", "sales.view_team", "sales.view_periods",
        "target.set", "coupon.view", "coupon.create", "coupon.disable", "campaign.manage",
        "markup.manage",
    },
    "finance": {
        "staff.login", "sales.view_periods",
        "expense.create", "expense.view", "finance.view_pnl", "finance.export",
    },
    "admin": set(ALL_PERMISSIONS),
}

VALID_ROLES = list(ROLE_PERMISSIONS.keys())


def normalize_roles(roles):
    """Unknown names dropped; 'staff' always present; NULL/empty -> ['staff']
    (least privilege, never admin)."""
    out = []
    for r in (roles if isinstance(roles, (list, tuple)) else []):
        r = str(r).strip().lower()
        if r in ROLE_PERMISSIONS and r not in out:
            out.append(r)
    if "staff" not in out:
        out.insert(0, "staff")
    return out


def permissions_for(roles):
    perms = set()
    for r in normalize_roles(roles):
        perms |= ROLE_PERMISSIONS[r]
    return perms
