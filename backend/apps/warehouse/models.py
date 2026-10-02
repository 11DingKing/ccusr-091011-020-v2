"""
库房管理模型
"""
from decimal import Decimal

from django.db import models
from django.utils import timezone
from apps.authentication.models import User


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    # 初始登记值：当前 quantity/batch_no 永远等于“初始值 + 有效更正链”的重建结果
    initial_quantity = models.DecimalField('原始入库数量', max_digits=12, decimal_places=2, default=Decimal('0'))
    initial_batch_no = models.CharField('原始批次号', max_length=50, blank=True, default='')
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']

    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"

    def save(self, *args, **kwargs):
        # 首次登记时以登记值初始化“原始值”，此后原始值永不改变，
        # 当前值只能通过更正分录生效事务来更新
        if self._state.adding:
            self.initial_quantity = self.quantity
            self.initial_batch_no = self.batch_no
        super().save(*args, **kwargs)


class StockInCorrection(models.Model):
    """
    入库记录追加式更正分录

    不可变事件链：每一行都是一次完整的状态快照（old_value/new_value）。
    主记录（StockIn.quantity/batch_no）只能在分录批准生效的同一事务内被更新，
    任何时候都可用“初始值 + 截至某时点已生效的分录”重建历史视图。
    """
    FIELD_CHOICES = [
        ('quantity', '入库数量'),
        ('batch_no', '批次号'),
    ]
    KIND_CHOICES = [
        ('correction', '更正'),
        ('reversal', '撤销更正'),
    ]
    STATUS_CHOICES = [
        ('proposed', '待批准'),
        ('effective', '已生效'),
        ('rejected', '已拒绝'),
    ]

    stock_in = models.ForeignKey(
        StockIn, on_delete=models.CASCADE,
        related_name='corrections', verbose_name='入库记录'
    )
    seq = models.PositiveIntegerField('链路序号')
    field_name = models.CharField('更正字段', max_length=20, choices=FIELD_CHOICES)
    kind = models.CharField('分录类型', max_length=20, choices=KIND_CHOICES, default='correction')
    # 统一以字符串存储：quantity 存 Decimal 的规范字符串，batch_no 原文存储
    old_value = models.CharField('原值', max_length=50)
    new_value = models.CharField('建议值', max_length=50)
    reason = models.TextField('更正理由')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='proposed')
    reject_reason = models.TextField('拒绝理由', blank=True, default='')

    proposed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='proposed_stock_in_corrections', verbose_name='申请人'
    )
    proposed_at = models.DateTimeField('申请时间', auto_now_add=True)
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_stock_in_corrections', verbose_name='批准人'
    )
    approved_at = models.DateTimeField('生效时间', null=True, blank=True)

    # 撤销分录指向被撤销的目标分录；目标分录生效时回填 reversed_by
    target = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reversals', verbose_name='撤销目标'
    )
    reversed_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reversed_entries', verbose_name='被撤销分录'
    )

    # 批准时对下游业务引用的快照（JSON 字符串），审计可解释为何放行
    downstream_refs = models.TextField('下游引用快照', blank=True, default='')

    class Meta:
        db_table = 'wh_stock_in_correction'
        verbose_name = '入库更正分录'
        verbose_name_plural = verbose_name
        ordering = ['stock_in_id', '-seq']
        constraints = [
            models.UniqueConstraint(
                fields=['stock_in', 'seq'],
                name='uniq_stock_in_correction_seq',
            ),
        ]

    def __str__(self):
        return f"入库#{self.stock_in_id} 更正#{self.seq} {self.field_name}"

    @property
    def field_label(self):
        return dict(self.FIELD_CHOICES).get(self.field_name, self.field_name)

    def decoded_new_value(self):
        """按字段类型还原建议值。"""
        if self.field_name == 'quantity':
            return Decimal(self.new_value)
        return self.new_value

    def decoded_old_value(self):
        if self.field_name == 'quantity':
            return Decimal(self.old_value)
        return self.old_value


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"
