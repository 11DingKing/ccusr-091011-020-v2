"""
入库记录更正领域服务

敏感字段（批次号、入库数量）不允许直接覆盖，一律通过追加式更正单变更：
提交 -> 审批生效（或拒绝/撤回），已生效的更正只能通过撤销分录冲回。
所有会同时改动主记录与更正链的操作都在单事务内完成，
任何一步失败都会整体回滚，不会留下只更新主记录却缺少更正链的状态。
"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.exceptions import BusinessException
from .models import Goods, StockIn, StockInCorrection, StockInCorrectionItem

logger = logging.getLogger('apps')

# 受更正链管控的敏感字段
SENSITIVE_FIELDS = {
    'batch_no': '批次号',
    'quantity': '入库数量',
}

QUANTITY_MAX = Decimal('9999999999.99')  # max_digits=12, decimal_places=2


def format_field_value(field_name, value):
    """把字段当前值规范化为更正链中存储的字符串形式"""
    if field_name == 'quantity':
        return str(value)
    return value or ''


def parse_field_value(field_name, raw):
    """把更正链中的字符串值还原为字段类型"""
    if field_name == 'quantity':
        return Decimal(raw)
    return raw


def validate_quantity(raw):
    """校验入库数量输入，返回规范化为两位小数的 Decimal"""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise BusinessException('入库数量格式不正确')
    if value <= 0:
        raise BusinessException('入库数量必须大于0')
    if value != value.quantize(Decimal('0.01')):
        raise BusinessException('入库数量最多保留两位小数')
    if value > QUANTITY_MAX:
        raise BusinessException('入库数量超出允许范围')
    return value.quantize(Decimal('0.01'))


def validate_field_value(field_name, raw):
    """校验单个敏感字段的建议值，返回规范化字符串"""
    if field_name not in SENSITIVE_FIELDS:
        raise BusinessException(f'字段 {field_name} 不支持更正，仅支持：批次号、入库数量')
    if field_name == 'quantity':
        return str(validate_quantity(raw))
    value = str(raw if raw is not None else '').strip()
    if len(value) > 50:
        raise BusinessException('批次号最多50个字符')
    return value


def is_stock_in_referenced(stock_in):
    """入库记录是否已被后续业务引用（其入库日期已生成每日报表）"""
    from apps.reports.models import DailyReport
    return DailyReport.objects.filter(report_date=stock_in.stock_in_time.date()).exists()


@transaction.atomic
def create_stock_in(goods_id, operator, quantity, batch_no='', supplier='', remark=''):
    """创建入库记录并同步增加货物库存（单事务）"""
    quantity = validate_quantity(quantity)
    goods = Goods.objects.select_for_update().filter(pk=goods_id, is_active=True).first()
    if not goods:
        raise BusinessException('货物不存在或已停用', code=404)

    stock_in = StockIn.objects.create(
        goods=goods,
        operator=operator,
        quantity=quantity,
        batch_no=batch_no or '',
        supplier=supplier or '',
        remark=remark or '',
    )
    goods.quantity = goods.quantity + quantity
    goods.save(update_fields=['quantity', 'updated_at'])

    logger.info(f"User {operator.username} created stock-in {stock_in.id} for goods {goods.code}")
    return stock_in


def _next_sequence(stock_in):
    """生成更正链内序号。调用方必须已持有入库记录行锁。"""
    current = stock_in.corrections.aggregate(max_seq=Max('sequence'))['max_seq']
    return (current or 0) + 1


def _create_correction(stock_in, items, reason, user, reverses=None):
    """在已持有 stock_in 行锁的事务内创建更正单及明细"""
    correction = StockInCorrection.objects.create(
        stock_in=stock_in,
        sequence=_next_sequence(stock_in),
        reason=reason,
        proposed_by=user,
        reverses=reverses,
        is_post_reference=is_stock_in_referenced(stock_in),
    )
    StockInCorrectionItem.objects.bulk_create([
        StockInCorrectionItem(
            correction=correction,
            field_name=item['field_name'],
            old_value=item['old_value'],
            new_value=item['new_value'],
        )
        for item in items
    ])
    return correction


def _normalize_items(stock_in, raw_items):
    """校验并规范化提交的更正明细，快照当前值作为原值"""
    if not raw_items:
        raise BusinessException('请至少提交一项更正内容')

    seen = set()
    normalized = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise BusinessException('更正内容格式不正确')
        field_name = raw.get('field_name')
        if field_name in seen:
            raise BusinessException(f'字段 {SENSITIVE_FIELDS.get(field_name, field_name)} 重复提交')
        seen.add(field_name)
        new_value = validate_field_value(field_name, raw.get('new_value'))
        old_value = format_field_value(field_name, getattr(stock_in, field_name))
        if new_value == old_value:
            raise BusinessException(f'{SENSITIVE_FIELDS[field_name]}的建议值与当前值一致，无需更正')
        normalized.append({
            'field_name': field_name,
            'old_value': old_value,
            'new_value': new_value,
        })
    return normalized


@transaction.atomic
def propose_correction(stock_in_id, raw_items, reason, user):
    """
    提交更正单（待审批）。

    连续更正规则：同一入库记录同一时刻只允许存在一笔待审批更正，
    前一笔生效、被拒绝或撤回后才能提交下一笔，保证链式追加不发生分叉。
    """
    reason = (reason or '').strip()
    if not reason:
        raise BusinessException('请填写更正理由')

    stock_in = StockIn.objects.select_for_update().filter(pk=stock_in_id).first()
    if not stock_in:
        raise BusinessException('入库记录不存在', code=404)

    if stock_in.corrections.filter(status='pending').exists():
        raise BusinessException('该记录存在待审批的更正单，请先处理后再提交新的更正')

    items = _normalize_items(stock_in, raw_items)
    correction = _create_correction(stock_in, items, reason, user)

    logger.info(f"User {user.username} proposed stock-in correction {correction.id} on record {stock_in_id}")
    return correction


@transaction.atomic
def approve_correction(correction_id, approver, remark=''):
    """
    批准更正单并使其生效（单事务）：
    校验 -> 更新主记录 -> 同步库存 -> 写入更正链生效状态，任一失败整体回滚。
    """
    correction = StockInCorrection.objects.select_for_update().filter(pk=correction_id).first()
    if not correction:
        raise BusinessException('更正单不存在', code=404)
    if correction.status != 'pending':
        raise BusinessException('该更正单已处理，无法重复审批')
    if correction.proposed_by_id == approver.id:
        raise BusinessException('提交人不能批准本人提交的更正单', code=403)

    stock_in = StockIn.objects.select_for_update().get(pk=correction.stock_in_id)
    items = list(correction.items.all())

    # 事后引用规则：记录已被后续业务引用时，数量更正须由管理员批准
    referenced = correction.is_post_reference or is_stock_in_referenced(stock_in)
    if referenced and any(item.field_name == 'quantity' for item in items) and not approver.is_admin:
        raise BusinessException('该记录已被后续业务引用，数量更正须由管理员批准', code=403)

    # 过期提案规则：原值必须与当前生效值一致，否则说明期间已有其他更正生效
    for item in items:
        current = format_field_value(item.field_name, getattr(stock_in, item.field_name))
        if current != item.old_value:
            raise BusinessException('更正所基于的记录状态已变化，请重新提交更正单', code=409)

    # 后续业务消耗规则：数量调减不得使货物库存为负
    quantity_delta = Decimal('0')
    for item in items:
        if item.field_name == 'quantity':
            quantity_delta = parse_field_value('quantity', item.new_value) - parse_field_value('quantity', item.old_value)

    goods = Goods.objects.select_for_update().get(pk=stock_in.goods_id)
    new_stock = goods.quantity + quantity_delta
    if new_stock < 0:
        raise BusinessException('更正后库存将为负数：该入库数量已被后续出库业务消耗，无法按建议值更正')

    for item in items:
        setattr(stock_in, item.field_name, parse_field_value(item.field_name, item.new_value))
    stock_in.save(update_fields=[item.field_name for item in items])

    if quantity_delta:
        goods.quantity = new_stock
        goods.save(update_fields=['quantity', 'updated_at'])

    correction.status = 'effective'
    correction.approved_by = approver
    correction.approver_remark = (remark or '').strip()
    correction.approved_at = timezone.now()
    correction.is_post_reference = referenced
    correction.save(update_fields=['status', 'approved_by', 'approver_remark', 'approved_at', 'is_post_reference'])

    logger.info(f"User {approver.username} approved stock-in correction {correction.id}")
    return correction


@transaction.atomic
def reject_correction(correction_id, approver, remark=''):
    """拒绝待审批更正单，主记录不受影响"""
    correction = StockInCorrection.objects.select_for_update().filter(pk=correction_id).first()
    if not correction:
        raise BusinessException('更正单不存在', code=404)
    if correction.status != 'pending':
        raise BusinessException('该更正单已处理，无法重复审批')
    if correction.proposed_by_id == approver.id:
        raise BusinessException('提交人不能审批本人提交的更正单', code=403)

    correction.status = 'rejected'
    correction.approved_by = approver
    correction.approver_remark = (remark or '').strip()
    correction.save(update_fields=['status', 'approved_by', 'approver_remark'])

    logger.info(f"User {approver.username} rejected stock-in correction {correction.id}")
    return correction


@transaction.atomic
def withdraw_correction(correction_id, user):
    """撤回待审批更正单（仅提交人本人），主记录不受影响"""
    correction = StockInCorrection.objects.select_for_update().filter(pk=correction_id).first()
    if not correction:
        raise BusinessException('更正单不存在', code=404)
    if correction.status != 'pending':
        raise BusinessException('仅待审批的更正单可以撤回')
    if correction.proposed_by_id != user.id:
        raise BusinessException('只有提交人可以撤回该更正单', code=403)

    correction.status = 'withdrawn'
    correction.save(update_fields=['status'])

    logger.info(f"User {user.username} withdrew stock-in correction {correction.id}")
    return correction


@transaction.atomic
def reverse_correction(correction_id, user, reason):
    """
    撤销已生效的更正：生成一笔方向相反的撤销分录（仍需审批生效）。

    规则：
    - 仅已生效的更正可被撤销，且最多被撤销一次；
    - 撤销分录本身不可再被撤销，如需恢复请提交新的普通更正单；
    - 若目标更正之后已有新的生效更正覆盖了相同字段，则不可直接撤销，
      应提交普通更正单，避免跳过链上中间状态。
    """
    reason = (reason or '').strip()
    if not reason:
        raise BusinessException('请填写撤销理由')

    target = StockInCorrection.objects.select_for_update().filter(pk=correction_id).first()
    if not target:
        raise BusinessException('更正单不存在', code=404)
    if target.status != 'effective':
        raise BusinessException('只能撤销已生效的更正单')
    if target.reverses_id:
        raise BusinessException('撤销分录本身不可再被撤销，如需恢复请提交新的更正单')
    if hasattr(target, 'reversed_by'):
        raise BusinessException('该更正单已被撤销，不能重复撤销')

    stock_in = StockIn.objects.select_for_update().get(pk=target.stock_in_id)
    if stock_in.corrections.filter(status='pending').exists():
        raise BusinessException('该记录存在待审批的更正单，请先处理后再提交撤销')

    items = []
    for item in target.items.all():
        current = format_field_value(item.field_name, getattr(stock_in, item.field_name))
        if current != item.new_value:
            raise BusinessException(
                f'{SENSITIVE_FIELDS[item.field_name]}已被后续更正再次变更，无法直接撤销，请提交普通更正单'
            )
        items.append({
            'field_name': item.field_name,
            'old_value': item.new_value,
            'new_value': item.old_value,
        })

    correction = _create_correction(stock_in, items, reason, user, reverses=target)

    logger.info(f"User {user.username} proposed reversal {correction.id} of correction {target.id}")
    return correction


def reconstruct_as_of(stock_in, as_of):
    """
    按时间点重建入库记录的旧视图：
    从当前状态出发，将在 as_of 之后生效的更正按从新到旧顺序逐条回退为原值。
    """
    values = {
        'batch_no': stock_in.batch_no,
        'quantity': stock_in.quantity,
    }
    later_corrections = (
        stock_in.corrections
        .filter(status='effective', approved_at__gt=as_of)
        .order_by('-sequence')
        .prefetch_related('items')
    )
    for correction in later_corrections:
        for item in correction.items.all():
            values[item.field_name] = item.old_value

    return {
        'batch_no': values['batch_no'],
        'quantity': parse_field_value('quantity', values['quantity'])
        if isinstance(values['quantity'], str) else values['quantity'],
    }
