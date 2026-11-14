"""YouTube 影片只處理一段：Article.range_start、Article.range_end（原影片的整數秒）。

兩個欄位都可以是空的，而且既有的資料列一律是空的＝沒有範圍：既有的影片都是整支處理的，不補任何值。
範圍存在時 range_start 有值（0＝從頭）、range_end 空＝一直到影片結尾。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("articles", "0015_multi_speaker"),
    ]

    operations = [
        migrations.AddField(
            model_name="article",
            name="range_start",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="article",
            name="range_end",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
    ]
