"""
入库记录追加式更正分录服务

不变量
------
1. StockIn.quantity / batch_no 是“当前状态缓存”，其值永远等于
   initial_* 依次叠加全部已生效且未被撤销的更正分录后的重建结果。
2. 主记录只能在分录批准生效的同一事务内更新；分录写入失败或校验
   失败一律回滚，不存在“主记录已改、更正链缺失”的中间状态。
3. 分录只追加、不删除、不就地改写（拒绝/撤销都产生新的状态行）。
"""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessException
from .models import Goods, StockIn, StockInCorrection, StockOut

CORRECTABLE_FIELDS = ('quantity', 'batch_no')
QUANTIZE = Decimal('0.01')
MAX_QUANTITY = Decimal('9999999999.99')


# ==================== 值编码与校验 ====================

def encode_value(field_name, value):
    """统一编码为分录中存储的字符串。"""
    if field_name == 'quantity':
        return str(Decimal(value).quantize(QUANTIZE))
    return '' if value is None else str(value)


def validate_new_value(field_name, value):
    """校验建议值，返回规范后的字符串形式。"""
    if value is None:
        raise BusinessException('请填写建议值')
    if field_name == 'quantity':
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError):
            raise BusinessException('建议数量必须是数字')
        if not number.is_finite():
            raise BusinessException('建议数量必须是有效数字')
        if number < 0:
            raise BusinessException('入库数量不能为负数')
        if number > MAX_QUANTITY:
            raise BusinessException('入库数量超出允许范围')
        if number.as_tuple().exponent < -2:
            raise BusinessException('入库数量最多保留两位小数')
        return str(number.quantize(QUANTIZE))
    text = str(value).strip()
    if len(text) > 50:
        raise BusinessException('批次号最多50个字')
    return text


def validate_reason(reason):
    if not reason or not str(reason).strip():
        raise BusinessException('请填写更正理由')
    return str(reason).strip()


def parse_moment(value):
    """解析 as_of 查询参数（日期或 ISO 时间），返回感知时区的 datetime。"""
    if value is None or value == '':
        return None
    text = str(value).strip()
    moment = None
    try:
        moment = datetime.strptime(text, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                moment = datetime.combine(
                    datetime.strptime(text, '%Y-%m-%d').date(),
                    datetime.max.time(),
                )
            except ValueError:
                raise BusinessException('时间点格式应为 YYYY-MM-DD 或 ISO 时间')
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


# ==================== 状态重建 ====================

def apply_entries(initial_quantity, initial_batch_no, entries, moment=None):
    """在给定初始值上按序应用已生效分录，重建字段状态（可批量复用）。"""
    values = {
        'quantity': Decimal(initial_quantity),
        'batch_no': initial_batch_no or '',
    }
    reversed_target_ids = {
        entry.target_id
        for entry in entries
        if entry.kind == 'reversal'
        and entry.target_id is not None
        and (moment is None or entry.approved_at <= moment)
    }
    for entry in entries:
        if entry.kind != 'correction':
            continue
        if moment is not None and entry.approved_at > moment:
            continue
        if entry.id in reversed_target_ids:
            continue
        if entry.field_name == 'quantity':
            values['quantity'] = Decimal(entry.new_value)
        else:
            values['batch_no'] = entry.new_value
    return values


def rebuild_values(stock_in, moment=None):
    """
    用“初始值 + 更正链”重建字段状态。

    moment 为 None 时重建当前状态；否则只应用 approved_at <= moment
    的生效事件，且该时刻之后才登记的入库记录由调用方自行过滤。
    """
    entries = list(
        stock_in.corrections.filter(status='effective').order_by('seq')
    )
    return apply_entries(
        stock_in.initial_quantity, stock_in.initial_batch_no, entries, moment
    )


def assert_chain_consistent(stock_in):
    """主记录缓存与更正链不一致时拒绝一切操作（数据损坏熔断）。"""
    rebuilt = rebuild_values(stock_in)
    if encode_value('quantity', rebuilt['quantity']) != encode_value('quantity', stock_in.quantity):
        raise BusinessException('入库主记录与更正链数量不一致，已锁定操作，请联系管理员', code=500)
    if rebuilt['batch_no'] != stock_in.batch_no:
        raise BusinessException('入库主记录与更正链批次号不一致，已锁定操作，请联系管理员', code=500)


# ==================== 下游业务引用 ====================

def collect_downstream_refs(stock_in):
    """
    收集该入库登记之后发生的、不可忽视的下游业务（非拒绝状态的出库）。

    被引用意味着该批入库数量/批次可能已随出库流转，更正必须由管理员
    批准，且批准时冻结一份引用快照，供审计解释放行依据。
    """
    refs = []
    downstream = (
        StockOut.objects
        .filter(goods_id=stock_in.goods_id, created_at__gte=stock_in.stock_in_time)
        .exclude(status='rejected')
        .select_related('operator')
        .order_by('created_at')
    )
    for out in downstream:
        refs.append({
            'stock_out_id': out.id,
            'quantity': str(Decimal(out.quantity).quantize(QUANTIZE)),
            'status': out.status,
            'receiver': out.receiver,
            'operator': out.operator.username if out.operator else None,
            'created_at': timezone.localtime(out.created_at).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return refs


def _latest_effective_correction(stock_in_id, field_name):
    """该字段当前最新一笔“已生效且未被撤销”的更正分录。"""
    return (
        StockInCorrection.objects
        .filter(
            stock_in_id=stock_in_id,
            field_name=field_name,
            kind='correction',
            status='effective',
            reversed_by__isnull=True,
        )
        .order_by('-seq')
        .first()
    )


def _next_seq(stock_in_id):
    last = StockInCorrection.objects.filter(stock_in_id=stock_in_id).order_by('-seq').first()
    return (last.seq + 1) if last else 1


def _has_pending(stock_in_id, field_name):
    return StockInCorrection.objects.filter(
        stock_in_id=stock_in_id, field_name=field_name, status='proposed'
    ).exists()


# ==================== 提议 ====================

@transaction.atomic
def propose_correction(stock_in_id, field_name, new_value, reason, user):
    """提议一笔更正分录（不改变主记录，等待批准）。"""
    if field_name not in CORRECTABLE_FIELDS:
        raise BusinessException('不支持更正该字段')
    reason = validate_reason(reason)

    stock_in = StockIn.objects.select_for_update().filter(pk=stock_in_id).first()
    if stock_in is None:
        raise BusinessException('入库记录不存在', code=404)
    assert_chain_consistent(stock_in)

    encoded_new = validate_new_value(field_name, new_value)
    encoded_old = encode_value(field_name, getattr(stock_in, field_name))
    if encoded_new == encoded_old:
        raise BusinessException('建议值与当前值一致，无需更正')
    if _has_pending(stock_in.id, field_name):
        raise BusinessException('该字段已有待批准的更正分录，请先处理后再提交', code=409)

    entry = StockInCorrection.objects.create(
        stock_in=stock_in,
        seq=_next_seq(stock_in.id),
        field_name=field_name,
        kind='correction',
        old_value=encoded_old,
        new_value=encoded_new,
        reason=reason,
        proposed_by=user,
    )
    return entry


@transaction.atomic
def propose_reversal(target_id, reason, user):
    """
    提议撤销一笔已生效的更正。

    只允许撤销该字段最新一笔已生效且未被撤销的更正；撤销本身也是
    追加分录（原值=当前值，建议值=被撤销分录的原值），需独立批准。
    """
    reason = validate_reason(reason)

    target = StockInCorrection.objects.filter(pk=target_id).first()
    if target is None:
        raise BusinessException('更正分录不存在', code=404)
    if target.kind != 'correction' or target.status != 'effective':
        raise BusinessException('只能撤销已生效的更正分录')
    if target.reversed_by_id is not None:
        raise BusinessException('该更正分录已被撤销', code=409)

    # 统一加锁顺序：先主记录，再分录行
    stock_in = StockIn.objects.select_for_update().get(pk=target.stock_in_id)
    target = StockInCorrection.objects.select_for_update().get(pk=target_id)
    assert_chain_consistent(stock_in)

    latest = _latest_effective_correction(stock_in.id, target.field_name)
    if latest is None or latest.id != target.id:
        raise BusinessException('只能撤销最新一笔已生效的更正，此前的更正请通过新增更正处理', code=409)
    if _has_pending(stock_in.id, target.field_name):
        raise BusinessException('该字段已有待批准的分录，请先处理后再提交', code=409)

    entry = StockInCorrection.objects.create(
        stock_in=stock_in,
        seq=_next_seq(stock_in.id),
        field_name=target.field_name,
        kind='reversal',
        target=target,
        old_value=target.new_value,
        new_value=target.old_value,
        reason=reason,
        proposed_by=user,
    )
    return entry


# ==================== 批准 / 拒绝 ====================

@transaction.atomic
def approve_correction(correction_id, approver):
    """批准分录并在同一事务内生效；任何失败整体回滚。"""
    entry = StockInCorrection.objects.filter(pk=correction_id).first()
    if entry is None:
        raise BusinessException('更正分录不存在', code=404)
    if entry.status != 'proposed':
        raise BusinessException('该分录已处理，不能重复批准', code=409)
    if entry.proposed_by_id and entry.proposed_by_id == approver.id:
        raise BusinessException('申请人不能批准本人提交的更正', code=403)

    # 统一加锁顺序：先主记录，再分录行
    stock_in = StockIn.objects.select_for_update().get(pk=entry.stock_in_id)
    entry = StockInCorrection.objects.select_for_update().get(pk=correction_id)
    if entry.status != 'proposed':
        raise BusinessException('该分录已处理，不能重复批准', code=409)
    assert_chain_consistent(stock_in)

    target = None
    if entry.kind == 'reversal':
        target = (
            StockInCorrection.objects
            .select_for_update()
            .filter(pk=entry.target_id)
            .first()
        )
        if target is None or target.status != 'effective' or target.reversed_by_id is not None:
            raise BusinessException('撤销目标不存在、未生效或已被撤销', code=409)
        latest = _latest_effective_correction(stock_in.id, entry.field_name)
        if latest is None or latest.id != target.id:
            raise BusinessException('只能撤销最新一笔已生效的更正', code=409)
        expected_old = target.new_value
        expected_new = target.old_value
    else:
        expected_old = encode_value(entry.field_name, getattr(stock_in, entry.field_name))
        expected_new = expected_old

    # 连续更正防线：分录记录的原值必须等于主记录当前值
    if entry.old_value != expected_old:
        raise BusinessException('原值与当前状态不一致，可能已被其他更正改变，请刷新后重新提交', code=409)
    if entry.kind == 'reversal' and entry.new_value != expected_new:
        raise BusinessException('撤销建议值与被撤销分录不符，请重新发起撤销', code=409)

    # 下游引用管控：存在引用时须管理员批准，并冻结引用快照
    refs = collect_downstream_refs(stock_in)
    if refs and not getattr(approver, 'is_admin', False):
        raise BusinessException('该入库记录已被后续出库业务引用，更正须由管理员批准', code=403)

    # 数量更正联动库存结余，按“建议值 - 当前值”调整；
    # 撤销更正等于反向增量。任何情况下更正后库存不得为负
    goods = None
    if entry.field_name == 'quantity':
        delta = entry.decoded_new_value() - stock_in.quantity
        goods = (
            Goods.objects.select_for_update()
            .get(pk=stock_in.goods_id)
        )
        if goods.quantity + delta < 0:
            raise BusinessException(
                f'更正后库存结余将为负（当前结余 {goods.quantity}，调整 {delta}），请核对出库记录后再更正'
            )

    now = timezone.now()
    new_value = entry.decoded_new_value()
    setattr(stock_in, entry.field_name, new_value)
    stock_in.save(update_fields=[entry.field_name])

    if goods is not None and delta != 0:
        goods.quantity = goods.quantity + delta
        goods.save(update_fields=['quantity', 'updated_at'])

    entry.status = 'effective'
    entry.approved_by = approver
    entry.approved_at = now
    entry.downstream_refs = json.dumps(refs, ensure_ascii=False)
    entry.save(update_fields=['status', 'approved_by', 'approved_at', 'downstream_refs'])

    if target is not None:
        target.reversed_by = entry
        target.save(update_fields=['reversed_by'])

    # 生效后立即自检：主记录必须能被更正链完整重建
    stock_in.refresh_from_db()
    assert_chain_consistent(stock_in)
    return entry


@transaction.atomic
def reject_correction(correction_id, approver, reject_reason):
    """拒绝分录：记录拒绝理由，主记录不变。"""
    reason = (reject_reason or '').strip()
    if not reason:
        raise BusinessException('请填写拒绝理由')
    entry = StockInCorrection.objects.filter(pk=correction_id).first()
    if entry is None:
        raise BusinessException('更正分录不存在', code=404)
    if entry.status != 'proposed':
        raise BusinessException('该分录已处理，不能重复操作', code=409)
    if entry.proposed_by_id and entry.proposed_by_id == approver.id:
        raise BusinessException('申请人不能审批本人提交的更正', code=403)

    # 统一加锁顺序：先主记录，再分录行
    StockIn.objects.select_for_update().get(pk=entry.stock_in_id)
    entry = StockInCorrection.objects.select_for_update().get(pk=correction_id)
    if entry.status != 'proposed':
        raise BusinessException('该分录已处理，不能重复操作', code=409)

    entry.status = 'rejected'
    entry.approved_by = approver
    entry.approved_at = timezone.now()
    entry.reject_reason = reason
    entry.save(update_fields=['status', 'approved_by', 'approved_at', 'reject_reason'])
    return entry
