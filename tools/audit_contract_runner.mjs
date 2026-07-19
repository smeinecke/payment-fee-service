import { PaymentFeeEngine } from "../packages/payment-fee-typescript/dist/index.js";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", async () => {
  const documents = JSON.parse(input);
  const engine = await PaymentFeeEngine.fromDocuments(documents);
  const counters = engine.auditContract();
  console.log(JSON.stringify({ counters, failures: [] }));
});
