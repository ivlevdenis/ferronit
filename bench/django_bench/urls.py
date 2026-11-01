from django.urls import path

from . import views

urlpatterns = [
    path("ping", views.ping),
    path("rows/any", views.rows_any),
    path("rows/upsert-any", views.upsert_any),
    path("rows/oldest", views.delete_oldest),
    path("rows/upsert", views.upsert),
    path("rows", views.rows),
    path("rows/<int:row_id>", views.rows_item),
]
