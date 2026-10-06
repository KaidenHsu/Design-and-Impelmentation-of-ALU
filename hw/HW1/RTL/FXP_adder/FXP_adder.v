module FXP_adder(
    input signed [31:0] a,
    input signed [31:0] b,
    output signed [32:0] d
);
    // Extend both signed operands before addition to retain the full sum.
    assign d = $signed({a[31], a}) + $signed({b[31], b});

endmodule
