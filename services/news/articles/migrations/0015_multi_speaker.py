"""YouTube 多人說話者區分：Article.speaker_count、Article.diarization、Slide.points。

speaker_count 分兩步：先用 default=1 加欄位，既有的資料列全部填 1（既有的 YouTube 影片都是用
`--speakers 1` 做的，也就是單人；議會的資料列忽略這個欄位）；再把預設改成 None。之後新登記的
影片沒有指定人數時就是 None＝多人模式、人數自動判斷。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("articles", "0014_youtube_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="article",
            name="speaker_count",
            field=models.PositiveIntegerField(blank=True, default=1, null=True),
        ),
        migrations.AlterField(
            model_name="article",
            name="speaker_count",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="article",
            name="diarization",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="slide",
            name="points",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
