# Generated for append-only stock-in corrections

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_initial_values(apps, schema_editor):
    """存量入库记录：以当前值作为原始登记值（此前无更正链）。"""
    StockIn = apps.get_model('warehouse', 'StockIn')
    for record in StockIn.objects.all():
        record.initial_quantity = record.quantity
        record.initial_batch_no = record.batch_no or ''
        record.save(update_fields=['initial_quantity', 'initial_batch_no'])


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('warehouse', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockin',
            name='initial_batch_no',
            field=models.CharField(blank=True, default='', max_length=50, verbose_name='原始批次号'),
        ),
        migrations.AddField(
            model_name='stockin',
            name='initial_quantity',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=12, verbose_name='原始入库数量'),
        ),
        migrations.RunPython(backfill_initial_values, reverse_backfill),
        migrations.CreateModel(
            name='StockInCorrection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('seq', models.PositiveIntegerField(verbose_name='链路序号')),
                ('field_name', models.CharField(choices=[('quantity', '入库数量'), ('batch_no', '批次号')], max_length=20, verbose_name='更正字段')),
                ('kind', models.CharField(choices=[('correction', '更正'), ('reversal', '撤销更正')], default='correction', max_length=20, verbose_name='分录类型')),
                ('old_value', models.CharField(max_length=50, verbose_name='原值')),
                ('new_value', models.CharField(max_length=50, verbose_name='建议值')),
                ('reason', models.TextField(verbose_name='更正理由')),
                ('status', models.CharField(choices=[('proposed', '待批准'), ('effective', '已生效'), ('rejected', '已拒绝')], default='proposed', max_length=20, verbose_name='状态')),
                ('reject_reason', models.TextField(blank=True, default='', verbose_name='拒绝理由')),
                ('proposed_at', models.DateTimeField(auto_now_add=True, verbose_name='申请时间')),
                ('approved_at', models.DateTimeField(blank=True, null=True, verbose_name='生效时间')),
                ('downstream_refs', models.TextField(blank=True, default='', verbose_name='下游引用快照')),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approved_stock_in_corrections', to=settings.AUTH_USER_MODEL, verbose_name='批准人')),
                ('proposed_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='proposed_stock_in_corrections', to=settings.AUTH_USER_MODEL, verbose_name='申请人')),
                ('reversed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reversed_entries', to='warehouse.stockincorrection', verbose_name='被撤销分录')),
                ('stock_in', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='corrections', to='warehouse.stockin', verbose_name='入库记录')),
                ('target', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reversals', to='warehouse.stockincorrection', verbose_name='撤销目标')),
            ],
            options={
                'verbose_name': '入库更正分录',
                'verbose_name_plural': '入库更正分录',
                'db_table': 'wh_stock_in_correction',
                'ordering': ['stock_in_id', '-seq'],
            },
        ),
        migrations.AddConstraint(
            model_name='stockincorrection',
            constraint=models.UniqueConstraint(fields=('stock_in', 'seq'), name='uniq_stock_in_correction_seq'),
        ),
    ]
