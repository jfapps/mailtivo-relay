from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.audit.models import AuditLog

from .forms import PoolForm, PoolMemberForm, WarmupForm
from .models import Pool, PoolMember, WarmupPlan


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
def list_view(request: HttpRequest) -> HttpResponse:
    pools = Pool.objects.all().prefetch_related("members__connection")
    return render(request, "pools/list.html", {"pools": pools})


@_admin_required
@require_http_methods(["GET", "POST"])
def create_view(request: HttpRequest) -> HttpResponse:
    form = PoolForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        pool = form.save()
        AuditLog.record(request.user, action="pool.created", target=pool.name, detail={"strategy": pool.routing_strategy})
        messages.success(request, f"Created pool “{pool.name}”.")
        return redirect(reverse("pools:edit", args=[pool.pk]))
    return render(request, "pools/edit.html", {"form": form, "creating": True})


@_admin_required
@require_http_methods(["GET", "POST"])
def edit_view(request: HttpRequest, pk: int) -> HttpResponse:
    pool = get_object_or_404(Pool, pk=pk)
    form = PoolForm(request.POST or None, instance=pool)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(request.user, action="pool.updated", target=pool.name)
        messages.success(request, "Pool saved.")
        return redirect(reverse("pools:edit", args=[pool.pk]))

    members = pool.members.select_related("connection").all()
    member_form = PoolMemberForm(pool=pool)
    plan = getattr(pool, "warmup_plan", None) or WarmupPlan(pool=pool)
    warmup_form = WarmupForm(instance=plan)
    return render(
        request,
        "pools/edit.html",
        {
            "form": form,
            "pool": pool,
            "creating": False,
            "members": members,
            "member_form": member_form,
            "warmup_form": warmup_form,
        },
    )


@_admin_required
@require_http_methods(["POST"])
def delete_view(request: HttpRequest, pk: int) -> HttpResponse:
    pool = get_object_or_404(Pool, pk=pk)
    name = pool.name
    pool.delete()
    AuditLog.record(request.user, action="pool.deleted", target=name)
    messages.success(request, f"Deleted pool “{name}”.")
    return redirect(reverse("pools:list"))


@_admin_required
@require_http_methods(["POST"])
def member_add(request: HttpRequest, pool_id: int) -> HttpResponse:
    pool = get_object_or_404(Pool, pk=pool_id)
    form = PoolMemberForm(request.POST, pool=pool)
    if form.is_valid():
        member: PoolMember = form.save(commit=False)
        member.pool = pool
        member.save()
        AuditLog.record(
            request.user,
            action="pool.member_added",
            target=f"{pool.name}/{member.connection.name}",
            detail={"weight": member.weight, "priority": member.priority},
        )
        messages.success(request, f"Added {member.connection.name} to pool.")
    else:
        messages.error(request, "Could not add member: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect(reverse("pools:edit", args=[pool.pk]))


@_admin_required
@require_http_methods(["POST"])
def member_update(request: HttpRequest, pool_id: int, member_id: int) -> HttpResponse:
    member = get_object_or_404(PoolMember, pk=member_id, pool_id=pool_id)
    before = {"weight": member.weight, "priority": member.priority, "enabled": member.enabled}
    if "weight" in request.POST:
        try:
            member.weight = max(0, int(request.POST["weight"]))
        except ValueError:
            return HttpResponse(status=400)
    if "priority" in request.POST:
        try:
            member.priority = max(0, int(request.POST["priority"]))
        except ValueError:
            return HttpResponse(status=400)
    if "enabled" in request.POST:
        member.enabled = request.POST["enabled"] in {"on", "true", "1"}
    member.save()
    AuditLog.record(
        request.user,
        action="pool.member_updated",
        target=f"{member.pool.name}/{member.connection.name}",
        detail={"before": before, "after": {"weight": member.weight, "priority": member.priority, "enabled": member.enabled}},
    )
    if request.headers.get("HX-Request"):
        return render(request, "pools/_member_row.html", {"m": member, "pool": member.pool})
    return redirect(reverse("pools:edit", args=[pool_id]))


@_admin_required
@require_http_methods(["POST"])
def member_remove(request: HttpRequest, pool_id: int, member_id: int) -> HttpResponse:
    member = get_object_or_404(PoolMember, pk=member_id, pool_id=pool_id)
    name = member.connection.name
    pool_name = member.pool.name
    member.delete()
    AuditLog.record(request.user, action="pool.member_removed", target=f"{pool_name}/{name}")
    messages.success(request, f"Removed {name} from pool.")
    return redirect(reverse("pools:edit", args=[pool_id]))


@_admin_required
@require_http_methods(["POST"])
def warmup_save(request: HttpRequest, pool_id: int) -> HttpResponse:
    pool = get_object_or_404(Pool, pk=pool_id)
    plan = getattr(pool, "warmup_plan", None) or WarmupPlan(pool=pool)
    form = WarmupForm(request.POST, instance=plan)
    if form.is_valid():
        plan = form.save(commit=False)
        plan.pool = pool
        plan.save()
        AuditLog.record(
            request.user,
            action="pool.warmup_saved",
            target=pool.name,
            detail={"curve": plan.curve, "enabled": plan.enabled},
        )
        messages.success(request, "Warm-up plan saved.")
    else:
        messages.error(request, "Could not save warm-up: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect(reverse("pools:edit", args=[pool.pk]))
