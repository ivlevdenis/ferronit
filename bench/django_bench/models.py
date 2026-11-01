from django.db import models


class BenchRow(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.TextField()
    email = models.TextField()
    created_at = models.DateTimeField()
    birthday = models.DateField()
    ttl = models.DurationField()

    class Meta:
        db_table = "bench_rows"
