# Order business rules

The test oracle: what the shop *should* do. Reports cite these IDs.

- **R1.** Paying moves an order from CREATED to PAID immediately and shows a payment confirmation to the customer.
- **R2.** Every paid order must reach ALLOCATED within 5 seconds, with no customer action; allocation is done by the system.
- **R3.** Only the warehouse ships an ALLOCATED order; only the courier marks a SHIPPED order delivered.
- **R4.** The order status shown to the customer must match the backend status within 2 seconds.
- **R5.** No step may produce a server error (HTTP 5xx) or a browser console error.
