module FXP_mul(
    input signed [31:0] a,
    input signed [31:0] b,
    output signed [63:0] d
);
    assign d = a * b;
endmodule