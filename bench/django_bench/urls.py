from django.urls import path

from . import views

urlpatterns = [
    path("ping", views.ping),
    path("rows/upsert", views.upsert),
    path("rows", views.rows),
    path("rows/<int:row_id>", views.rows_item),
]
