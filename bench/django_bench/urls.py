from django.urls import path

from . import views

urlpatterns = [path("rows", views.rows), path("ping", views.ping)]
