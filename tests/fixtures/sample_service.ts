/**
 * OrderController — handles HTTP requests for order management.
 *
 * This is a sample TypeScript fixture for extraction testing.
 * Represents a controller with route handlers and service dependencies.
 */

import { Request, Response, Router } from 'express';
import { OrderService } from '../services/OrderService';
import { ShipmentService } from '../services/ShipmentService';
import { Logger } from '../utils/logger';

interface CreateOrderRequest {
  customerId: string;
  items: Array<{ productId: string; quantity: number; price: number }>;
  carrierToken: string;
}

interface OrderResponse {
  orderId: string;
  status: string;
  total: number;
  createdAt: string;
}

export class OrderController {
  private readonly router: Router;
  private readonly orderService: OrderService;
  private readonly shipmentService: ShipmentService;
  private readonly logger: Logger;

  constructor(
    orderService: OrderService,
    shipmentService: ShipmentService,
    logger: Logger,
  ) {
    this.router = Router();
    this.orderService = orderService;
    this.shipmentService = shipmentService;
    this.logger = logger;
    this.setupRoutes();
  }

  private setupRoutes(): void {
    this.router.post('/orders', this.createOrder.bind(this));
    this.router.get('/orders/:id', this.getOrder.bind(this));
    this.router.post('/orders/:id/cancel', this.cancelOrder.bind(this));
  }

  async createOrder(req: Request, res: Response): Promise<void> {
    const body = req.body as CreateOrderRequest;
    this.logger.info('Creating order for customer', { customerId: body.customerId });

    const order = await this.orderService.create(body.customerId, body.items);
    const shipment = await this.shipmentService.createShipment(
      order.id,
      order.total,
      'standard',
      body.carrierToken,
    );

    const response: OrderResponse = {
      orderId: order.id,
      status: shipment.status === 'completed' ? 'confirmed' : 'pending',
      total: order.total,
      createdAt: order.createdAt.toISOString(),
    };

    res.status(201).json(response);
  }

  async getOrder(req: Request, res: Response): Promise<void> {
    const order = await this.orderService.findById(req.params.id);
    if (!order) {
      res.status(404).json({ error: 'Order not found' });
      return;
    }
    res.json(order);
  }

  async cancelOrder(req: Request, res: Response): Promise<void> {
    await this.orderService.cancel(req.params.id);
    await this.shipmentService.cancelShipment(req.params.id);
    res.status(200).json({ status: 'cancelled' });
  }

  getRouter(): Router {
    return this.router;
  }
}
