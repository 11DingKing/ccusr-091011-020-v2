"""
入库记录追加式更正分录测试
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    Approval, Category, Goods, StockIn, StockInCorrection, StockOut, Unit, Variety,
)
from apps.warehouse import corrections as service
from django.test import TransactionTestCase


class CorrectionFixture(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.admin = User.objects.create_user("corr-admin", "pass123456", role="admin")
        self.admin2 = User.objects.create_user("corr-admin-2", "pass123456", role="admin")
        self.clerk = User.objects.create_user("corr-clerk", "pass123456", role="user")
        self.admin_client = self._client(self.admin)
        self.admin2_client = self._client(self.admin2)
        self.clerk_client = self._client(self.clerk)
        self.unit = Unit.objects.create(name="件", created_by=self.admin)
        self.category = Category.objects.create(name="监管器材", unit=self.unit, created_by=self.admin)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.admin)
        self.goods = Goods.objects.create(
            variety=self.variety, name="执法终端", code="CORR-001",
            quantity=Decimal("0"), warning_threshold=Decimal("5"),
        )

    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def create_stock_in(self, quantity="10", batch_no="B-2026-001"):
        response = self.admin_client.post("/api/stock-in/", {
            "goods": self.goods.id,
            "quantity": quantity,
            "batch_no": batch_no,
            "supplier": "供应商甲",
        }, format="json")
        assert response.status_code == 200, response.json()
        return StockIn.objects.get(pk=response.json()["data"]["id"])

    def propose(self, client, stock_in_id, field_name, new_value, reason="录错更正"):
        return client.post(f"/api/stock-in/{stock_in_id}/corrections/", {
            "field_name": field_name,
            "new_value": new_value,
            "reason": reason,
        }, format="json")

    def approve(self, client, correction_id):
        return client.post(f"/api/corrections/{correction_id}/approve/")

    def reject(self, client, correction_id, reason="证据不足"):
        return client.post(f"/api/corrections/{correction_id}/reject/", {"reject_reason": reason}, format="json")

    def propose_reverse(self, client, correction_id, reason="撤销"):
        return client.post(f"/api/corrections/{correction_id}/reverse-proposal/", {"reason": reason}, format="json")


class StockInRegistrationTest(CorrectionFixture):
    def test_registration_initializes_original_values_and_stock(self):
        stock_in = self.create_stock_in("10", "B-001")
        self.assertEqual(stock_in.initial_quantity, Decimal("10.00"))
        self.assertEqual(stock_in.initial_batch_no, "B-001")
        self.assertEqual(stock_in.quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

    def test_requires_authentication(self):
        response = APIClient().get("/api/stock-in/")
        self.assertEqual(response.status_code, 401)


class CorrectionProposalTest(CorrectionFixture):
    def test_propose_does_not_change_main_record(self):
        stock_in = self.create_stock_in()
        response = self.propose(self.clerk_client, stock_in.id, "quantity", "12")
        self.assertEqual(response.status_code, 200)
        entry_id = response.json()["data"]["id"]

        entry = StockInCorrection.objects.get(pk=entry_id)
        self.assertEqual(entry.status, "proposed")
        self.assertEqual(entry.old_value, "10.00")
        self.assertEqual(entry.new_value, "12.00")
        self.assertEqual(entry.proposed_by_id, self.clerk.id)
        self.assertIsNone(entry.approved_by_id)

        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

    def test_same_value_and_invalid_value_rejected(self):
        stock_in = self.create_stock_in()
        same = self.propose(self.clerk_client, stock_in.id, "quantity", "10")
        self.assertEqual(same.status_code, 400)
        negative = self.propose(self.clerk_client, stock_in.id, "quantity", "-1")
        self.assertEqual(negative.status_code, 400)
        text = self.propose(self.clerk_client, stock_in.id, "quantity", "abc")
        self.assertEqual(text.status_code, 400)
        too_long = self.propose(self.clerk_client, stock_in.id, "batch_no", "B" * 51)
        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(StockInCorrection.objects.count(), 0)

    def test_pending_entry_blocks_next_proposal_on_same_field(self):
        stock_in = self.create_stock_in()
        first = self.propose(self.clerk_client, stock_in.id, "quantity", "12")
        self.assertEqual(first.status_code, 200)
        second = self.propose(self.admin_client, stock_in.id, "quantity", "14")
        self.assertEqual(second.status_code, 409)
        # 不同字段仍可提议
        batch = self.propose(self.clerk_client, stock_in.id, "batch_no", "B-NEW")
        self.assertEqual(batch.status_code, 200)

    def test_proposer_cannot_approve_own_entry(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        response = self.approve(self.clerk_client, entry_id)
        self.assertEqual(response.status_code, 403)
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))


class CorrectionApproveRejectTest(CorrectionFixture):
    def test_approve_effects_main_record_stock_and_chain(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]

        response = self.approve(self.admin_client, entry_id)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["status"], "effective")
        self.assertEqual(data["approved_by_name"], "corr-admin")
        self.assertIsNotNone(data["approved_at"])

        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("12.00"))
        # 原始值永不改变
        self.assertEqual(stock_in.initial_quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("12.00"))

    def test_batch_no_correction_keeps_stock_unchanged(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "batch_no", "B-FIXED").json()["data"]["id"]
        self.approve(self.admin_client, entry_id)
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.batch_no, "B-FIXED")
        self.assertEqual(stock_in.initial_batch_no, "B-2026-001")
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

    def test_reject_requires_reason_and_leaves_state_intact(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        missing = self.admin_client.post(f"/api/corrections/{entry_id}/reject/", {}, format="json")
        self.assertEqual(missing.status_code, 400)

        response = self.reject(self.admin_client, entry_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "rejected")

        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

        # 拒绝后同字段允许重新提议
        again = self.propose(self.clerk_client, stock_in.id, "quantity", "13")
        self.assertEqual(again.status_code, 200)

    def test_double_approve_rejected(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        self.approve(self.admin_client, entry_id)
        again = self.approve(self.admin_client, entry_id)
        self.assertEqual(again.status_code, 409)


class ConsecutiveCorrectionTest(CorrectionFixture):
    def test_consecutive_corrections_form_linear_chain(self):
        stock_in = self.create_stock_in()

        first_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        self.approve(self.admin_client, first_id)
        second_id = self.propose(self.clerk_client, stock_in.id, "quantity", "8").json()["data"]["id"]
        # 第二笔原值必须等于第一笔生效后的当前值
        self.assertEqual(
            StockInCorrection.objects.get(pk=second_id).old_value, "12.00"
        )
        self.approve(self.admin_client, second_id)

        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("8.00"))
        seqs = list(StockInCorrection.objects.order_by("seq").values_list("seq", flat=True))
        self.assertEqual(seqs, [1, 2])

        rebuilt = service.rebuild_values(stock_in)
        self.assertEqual(rebuilt["quantity"], Decimal("8.00"))


class ReversalTest(CorrectionFixture):
    def _two_effective_corrections(self):
        stock_in = self.create_stock_in()
        first_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        self.approve(self.admin_client, first_id)
        second_id = self.propose(self.clerk_client, stock_in.id, "quantity", "8").json()["data"]["id"]
        self.approve(self.admin_client, second_id)
        return stock_in, first_id, second_id

    def test_only_latest_effective_correction_can_be_reversed(self):
        stock_in, first_id, second_id = self._two_effective_corrections()
        # 第一笔不是最新生效更正，不能撤销
        stale = self.propose_reverse(self.clerk_client, first_id)
        self.assertEqual(stale.status_code, 409)

        rev_id = self.propose_reverse(self.clerk_client, second_id).json()["data"]["id"]
        reversal = StockInCorrection.objects.get(pk=rev_id)
        self.assertEqual(reversal.kind, "reversal")
        self.assertEqual(reversal.old_value, "8.00")
        self.assertEqual(reversal.new_value, "12.00")
        self.assertEqual(reversal.target_id, second_id)

        # 撤销批准前主记录不动
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("8.00"))
        self.approve(self.admin_client, rev_id)

        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("12.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("12.00"))
        self.assertEqual(
            StockInCorrection.objects.get(pk=second_id).reversed_by_id, rev_id
        )

        # 现在第一笔成为最新生效更正，可以继续撤销
        rev2_id = self.propose_reverse(self.clerk_client, first_id).json()["data"]["id"]
        self.approve(self.admin_client, rev2_id)
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

    def test_cannot_reverse_twice_or_reverse_reversal(self):
        stock_in, first_id, second_id = self._two_effective_corrections()
        rev_id = self.propose_reverse(self.clerk_client, second_id).json()["data"]["id"]
        # 挂起的撤销存在时不能重复发起
        duplicate = self.propose_reverse(self.admin_client, second_id)
        self.assertEqual(duplicate.status_code, 409)
        self.approve(self.admin_client, rev_id)
        again = self.propose_reverse(self.clerk_client, second_id)
        self.assertEqual(again.status_code, 409)

    def test_reversal_rebuilds_point_in_time(self):
        stock_in, first_id, second_id = self._two_effective_corrections()
        rev_id = self.propose_reverse(self.clerk_client, second_id).json()["data"]["id"]
        self.approve(self.admin_client, rev_id)

        stock_in.refresh_from_db()
        # 撤销生效时刻：当前值 12；撤销生效之前：值 8。
        # 各批准可能发生在同一秒内，直接取第二笔更正的生效时刻作为确定性边界
        second_approved_at = StockInCorrection.objects.get(pk=second_id).approved_at
        rev_effective_at = StockInCorrection.objects.get(pk=rev_id).approved_at
        self.assertGreater(rev_effective_at, second_approved_at)
        self.assertEqual(
            service.rebuild_values(stock_in, second_approved_at)["quantity"], Decimal("8.00")
        )
        self.assertEqual(
            service.rebuild_values(stock_in)["quantity"], Decimal("12.00")
        )


class DownstreamReferenceTest(CorrectionFixture):
    def test_referenced_record_requires_admin_and_freezes_snapshot(self):
        stock_in = self.create_stock_in()
        stock_out = StockOut.objects.create(
            goods=self.goods, operator=self.clerk,
            receiver="领用民警", quantity=Decimal("3"), status="completed",
        )
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]

        # 普通用户（即使是另一人）不能批准
        another_clerk = User.objects.create_user("corr-clerk-2", "pass123456", role="user")
        forbidden = self.approve(self._client(another_clerk), entry_id)
        self.assertEqual(forbidden.status_code, 403)

        allowed = self.approve(self.admin_client, entry_id)
        self.assertEqual(allowed.status_code, 200)
        entry = StockInCorrection.objects.get(pk=entry_id)
        snapshot = json.loads(entry.downstream_refs)
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["stock_out_id"], stock_out.id)
        self.assertEqual(snapshot[0]["quantity"], "3.00")

    def test_rejected_stock_out_does_not_count_as_reference(self):
        stock_in = self.create_stock_in()
        StockOut.objects.create(
            goods=self.goods, operator=self.clerk,
            receiver="领用民警", quantity=Decimal("3"), status="rejected",
        )
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        another_clerk = User.objects.create_user("corr-clerk-3", "pass123456", role="user")
        response = self.approve(self._client(another_clerk), entry_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(StockInCorrection.objects.get(pk=entry_id).downstream_refs), []
        )


class AtomicityTest(CorrectionFixture):
    def test_failed_approval_leaves_no_partial_state(self):
        stock_in = self.create_stock_in()
        # 制造下游引用
        StockOut.objects.create(
            goods=self.goods, operator=self.clerk,
            receiver="领用民警", quantity=Decimal("3"), status="completed",
        )
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]

        another_clerk = User.objects.create_user("corr-clerk-4", "pass123456", role="user")
        response = self.approve(self._client(another_clerk), entry_id)
        self.assertEqual(response.status_code, 403)

        # 分录仍为 proposed，主记录与库存均未变
        self.assertEqual(StockInCorrection.objects.get(pk=entry_id).status, "proposed")
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10.00"))

    def test_negative_stock_balance_blocks_quantity_correction(self):
        stock_in = self.create_stock_in("10")
        # 模拟出库已扣减库存：结余只剩 4，入库从 10 改为 0 会使结余为负
        Goods.objects.filter(pk=self.goods.id).update(quantity=Decimal("4"))
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "0").json()["data"]["id"]
        response = self.approve(self.admin_client, entry_id)
        self.assertEqual(response.status_code, 400)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("4"))
        stock_in.refresh_from_db()
        self.assertEqual(stock_in.quantity, Decimal("10.00"))


class PointInTimeQueryTest(CorrectionFixture):
    def test_list_default_shows_latest_and_as_of_rebuilds_old_view(self):
        stock_in = self.create_stock_in("10", "B-OLD")
        # 入库时间回溯到 9 月 1 日，更正生效发生在 9 月 15 日之后
        old_time = timezone.now() - timedelta(days=30)
        StockIn.objects.filter(pk=stock_in.id).update(stock_in_time=old_time)

        qty_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        batch_id = self.propose(self.clerk_client, stock_in.id, "batch_no", "B-NEW").json()["data"]["id"]
        self.approve(self.admin_client, qty_id)
        self.approve(self.admin_client, batch_id)

        # 默认：最新状态
        latest = self.admin_client.get("/api/stock-in/").json()["data"]["list"][0]
        self.assertEqual(latest["quantity"], "12.00")
        self.assertEqual(latest["batch_no"], "B-NEW")
        self.assertTrue(latest["has_correction"])
        self.assertEqual(latest["correction_count"], 2)

        # 时间点（入库之后、更正之前）：旧视图
        middle = (old_time + timedelta(days=10)).strftime("%Y-%m-%d")
        old_view = self.admin_client.get(f"/api/stock-in/?as_of={middle}").json()["data"]
        self.assertEqual(old_view["as_of"], middle)
        row = old_view["list"][0]
        self.assertEqual(row["quantity"], "10.00")
        self.assertEqual(row["batch_no"], "B-OLD")

        # 更早时间点：记录尚不存在
        before = (old_time - timedelta(days=10)).strftime("%Y-%m-%d")
        empty = self.admin_client.get(f"/api/stock-in/?as_of={before}").json()["data"]
        self.assertEqual(empty["total"], 0)

    def test_daily_report_as_of_rebuilds_totals(self):
        stock_in = self.create_stock_in("10")
        old_dt = timezone.now() - timedelta(days=30)
        StockIn.objects.filter(pk=stock_in.id).update(stock_in_time=old_dt)
        old_day = timezone.localtime(old_dt).date()
        qty_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        self.approve(self.admin_client, qty_id)

        middle = (old_day + timedelta(days=10)).strftime("%Y-%m-%d")
        old_report = self.admin_client.get(
            f"/api/daily-report/?start_date={old_day}&end_date={old_day}&as_of={middle}"
        ).json()["data"][0]
        self.assertEqual(old_report["in_total"], 10.0)

        latest_report = self.admin_client.get(
            f"/api/daily-report/?start_date={old_day}&end_date={old_day}"
        ).json()["data"][0]
        self.assertEqual(latest_report["in_total"], 12.0)

    def test_invalid_as_of_returns_400(self):
        response = self.admin_client.get("/api/stock-in/?as_of=not-a-date")
        self.assertEqual(response.status_code, 400)


class CorrectionChainQueryAndExportTest(CorrectionFixture):
    def test_chain_listing_order(self):
        stock_in = self.create_stock_in()
        first = self.propose(self.clerk_client, stock_in.id, "quantity", "12")
        self.approve(self.admin_client, first.json()["data"]["id"])
        second = self.propose(self.clerk_client, stock_in.id, "batch_no", "B-NEW")

        response = self.admin_client.get(f"/api/stock-in/{stock_in.id}/corrections/")
        self.assertEqual(response.status_code, 200)
        chain = response.json()["data"]
        self.assertEqual([c["seq"] for c in chain], [1, 2])
        self.assertEqual(chain[0]["status"], "effective")
        self.assertEqual(chain[1]["status"], "proposed")
        self.assertEqual(chain[0]["field_label"], "入库数量")

        missing = self.admin_client.get("/api/stock-in/99999/corrections/")
        self.assertEqual(missing.status_code, 404)

    def test_correction_audit_export(self):
        stock_in = self.create_stock_in()
        entry_id = self.propose(self.clerk_client, stock_in.id, "quantity", "12").json()["data"]["id"]
        self.approve(self.admin_client, entry_id)

        response = self.admin_client.get("/api/export/?type=stock_in_corrections")
        self.assertEqual(response.status_code, 200)
        # Django 对中文文件名做 RFC 2047 编码，解码后校验
        from email.header import decode_header
        disposition = response["Content-Disposition"]
        decoded = "".join(
            part.decode(enc or "ascii") if isinstance(part, bytes) else part
            for part, enc in decode_header(disposition)
        )
        self.assertTrue(decoded.startswith("attachment"))
        self.assertIn("入库更正分录", decoded)
        # openpyxl 可打开且包含分录行
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(response.content))
        ws = wb.active
        self.assertEqual(ws.title, "入库更正分录")
        body = [list(row) for row in ws.iter_rows(min_row=2, values_only=True)]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0][0], stock_in.id)
        self.assertEqual(body[0][3], 1)
        self.assertEqual(body[0][4], "入库数量")
        self.assertEqual(body[0][6], "10.00")
        self.assertEqual(body[0][7], "12.00")
