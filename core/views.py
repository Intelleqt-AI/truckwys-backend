from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from .models import (
    User, Customer, Driver, Vehicle, VehicleLog, Load,
    Quote, Invoice, Payment, Expense, Settlement, Notification
)
from .serializers import (
    UserSerializer, CustomerSerializer, DriverSerializer,
    VehicleSerializer, VehicleLogSerializer, LoadSerializer,
    QuoteSerializer, InvoiceSerializer, PaymentSerializer,
    ExpenseSerializer, SettlementSerializer, NotificationSerializer
)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            user.set_password(request.data.get('password'))
            user.save()
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': UserSerializer(user).data
            }, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        
        user = authenticate(username=username, password=password)
        if user:
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': UserSerializer(user).data
            })
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_401_UNAUTHORIZED
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        request.user.auth_token.delete()
        return Response({'message': 'Successfully logged out'})


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['role', 'is_active']
    search_fields = ['username', 'email', 'first_name', 'last_name']
    ordering_fields = ['created_at', 'username']


class CustomerViewSet(viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'city', 'state']
    search_fields = ['name', 'company', 'email', 'phone']
    ordering_fields = ['created_at', 'name']

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads for a specific customer"""
        customer = self.get_object()
        loads = customer.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def invoices(self, request, pk=None):
        """Get all invoices for a specific customer"""
        customer = self.get_object()
        invoices = customer.invoices.all()
        serializer = InvoiceSerializer(invoices, many=True)
        return Response(serializer.data)


class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'license_state']
    search_fields = ['user__username', 'license_number', 'user__first_name', 'user__last_name']
    ordering_fields = ['created_at', 'hire_date']

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads assigned to a driver"""
        driver = self.get_object()
        loads = driver.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def settlements(self, request, pk=None):
        """Get all settlements for a driver"""
        driver = self.get_object()
        settlements = driver.settlements.all()
        serializer = SettlementSerializer(settlements, many=True)
        return Response(serializer.data)


class VehicleViewSet(viewsets.ModelViewSet):
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'type', 'fuel_type']
    search_fields = ['vin', 'plate', 'make', 'model']
    ordering_fields = ['created_at', 'make', 'model', 'year']

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """Get all logs for a specific vehicle"""
        vehicle = self.get_object()
        logs = vehicle.logs.all()
        serializer = VehicleLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def loads(self, request, pk=None):
        """Get all loads assigned to a vehicle"""
        vehicle = self.get_object()
        loads = vehicle.loads.all()
        serializer = LoadSerializer(loads, many=True)
        return Response(serializer.data)


class VehicleLogViewSet(viewsets.ModelViewSet):
    queryset = VehicleLog.objects.all()
    serializer_class = VehicleLogSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['vehicle', 'log_type', 'date']
    search_fields = ['description', 'vehicle__vin', 'vehicle__plate']
    ordering_fields = ['date', 'cost']

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class LoadViewSet(viewsets.ModelViewSet):
    queryset = Load.objects.all()
    serializer_class = LoadSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer', 'driver', 'vehicle']
    search_fields = ['load_number', 'pickup_city', 'delivery_city', 'cargo_description']
    ordering_fields = ['created_at', 'pickup_date', 'delivery_date']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """Update load status"""
        load = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in dict(Load.STATUS_CHOICES).keys():
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        load.status = new_status
        load.save()
        serializer = self.get_serializer(load)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def assign_driver(self, request, pk=None):
        """Assign driver and vehicle to load"""
        load = self.get_object()
        driver_id = request.data.get('driver_id')
        vehicle_id = request.data.get('vehicle_id')
        
        try:
            load.driver_id = driver_id
            load.vehicle_id = vehicle_id
            load.status = 'ASSIGNED'
            load.save()
            serializer = self.get_serializer(load)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class QuoteViewSet(viewsets.ModelViewSet):
    queryset = Quote.objects.all()
    serializer_class = QuoteSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer']
    search_fields = ['quote_number', 'customer__name', 'pickup_location', 'delivery_location']
    ordering_fields = ['created_at', 'valid_until']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """Update quote status"""
        quote = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in dict(Quote.STATUS_CHOICES).keys():
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        quote.status = new_status
        quote.save()
        serializer = self.get_serializer(quote)
        return Response(serializer.data)


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer', 'load']
    search_fields = ['invoice_number', 'customer__name']
    ordering_fields = ['created_at', 'issue_date', 'due_date']

    @action(detail=True, methods=['get'])
    def payments(self, request, pk=None):
        """Get all payments for an invoice"""
        invoice = self.get_object()
        payments = invoice.payments.all()
        serializer = PaymentSerializer(payments, many=True)
        return Response(serializer.data)


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['payment_method', 'customer', 'invoice']
    search_fields = ['payment_number', 'reference_number', 'customer__name']
    ordering_fields = ['payment_date', 'amount']


class ExpenseViewSet(viewsets.ModelViewSet):
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'vehicle', 'driver']
    search_fields = ['expense_number', 'description', 'vendor']
    ordering_fields = ['expense_date', 'amount']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class SettlementViewSet(viewsets.ModelViewSet):
    queryset = Settlement.objects.all()
    serializer_class = SettlementSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'driver']
    search_fields = ['settlement_number', 'driver__user__username']
    ordering_fields = ['created_at', 'start_date', 'end_date']

    @action(detail=True, methods=['patch'])
    def approve(self, request, pk=None):
        """Approve a settlement"""
        settlement = self.get_object()
        settlement.status = 'APPROVED'
        settlement.save()
        serializer = self.get_serializer(settlement)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'])
    def mark_paid(self, request, pk=None):
        """Mark settlement as paid"""
        settlement = self.get_object()
        settlement.status = 'PAID'
        settlement.payment_date = timezone.now().date()
        settlement.save()
        serializer = self.get_serializer(settlement)
        return Response(serializer.data)


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['type', 'is_read']
    ordering_fields = ['created_at']

    def get_queryset(self):
        """Return notifications for the current user"""
        return Notification.objects.filter(user=self.request.user)

    @action(detail=True, methods=['patch'])
    def mark_read(self, request, pk=None):
        """Mark notification as read"""
        notification = self.get_object()
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save()
        serializer = self.get_serializer(notification)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark all notifications as read"""
        self.get_queryset().update(is_read=True, read_at=timezone.now())
        return Response({'message': 'All notifications marked as read'})