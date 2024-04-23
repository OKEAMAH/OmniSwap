import json
import os
import time
from random import choice

import ccxt
import requests
from brownie import Contract, web3
from brownie.project.main import Project
from retrying import retry

from scripts.kyberswap import kyber_calldata
from scripts.helpful_scripts import (
    get_account,
    zero_address,
    combine_bytes,
    padding_to_bytes,
    Session,
    get_token_address,
    get_token_decimal,
    get_chain_id,
    get_swap_info,
    to_hex_str,
    get_account_address,
    get_connext_domain_id,
    get_connext_execute_gas,
    get_connext_execute_l1_gas,
)

uniswap_v3_fee_decimal = 1e6

root_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

src_session: Session = None
dst_session: Session = None


def get_contract(contract_name: str, p: Project = None):
    return p[contract_name]


def get_contract_address(contract_name: str, p: Project = None):
    return get_contract(contract_name, p)[-1].address


def token_approve(
    token_name: str, aprrove_address: str, amount: int, p: Project = None
):
    token = Contract.from_abi(
        token_name.upper(), get_token_address(token_name), p.interface.IERC20.abi
    )
    token.approve(aprrove_address, amount, {"from": get_account()})


def get_token_amount_decimal(token):
    if token in ["eth", "matic", "bnb"]:
        return 18
    elif token == "apt":
        return 8
    elif token == "sui":
        return 9


def get_network_token(network):
    return "matic" if "polygon" in network else "eth"


def get_fee_value(amount, token="sui"):
    price = get_token_price(token)
    decimal = get_token_amount_decimal(token)
    return price * amount / pow(10, decimal)


def get_fee_amount(value, token="sui"):
    price = get_token_price(token)
    decimal = get_token_amount_decimal(token)
    return int(value / price * pow(10, decimal))


class View:
    def __repr__(self):
        data = vars(self)
        for k in list(data.keys()):
            if not k.startswith("_"):
                continue
            del data[k]
        return json.dumps(data, sort_keys=True, indent=4, separators=(",", ":"))

    @staticmethod
    def from_dict(obj, data: dict):
        return obj(**data)


class SoData(View):
    def __init__(
        self,
        transactionId,
        receiver,
        sourceChainId,
        sendingAssetId,
        destinationChainId,
        receivingAssetId,
        amount,
    ):
        # unique identification id
        self.transactionId = transactionId
        # token receiving account
        self.receiver = receiver
        # source chain id
        self.sourceChainId = sourceChainId
        # The starting token address of the source chain
        self.sendingAssetId = sendingAssetId
        # destination chain id
        self.destinationChainId = destinationChainId
        # The final token address of the destination chain
        self.receivingAssetId = receivingAssetId
        # User enters amount
        self.amount = amount

    def format_to_contract(self):
        """Get the SoData needed for the contract interface

        Returns:
            SoData: Information for recording and tracking cross-chain transactions
        """
        return [
            self.transactionId,
            self.receiver,
            self.sourceChainId,
            self.sendingAssetId,
            self.destinationChainId if self.destinationChainId < 65535 else 0,
            self.receivingAssetId,
            self.amount,
        ]

    @staticmethod
    def generate_random_bytes32():
        """Produce random transactions iD for tracking transactions on both chains

        Returns:
            result: 32 bytes hex
        """
        chars = [str(i) for i in range(10)] + ["a", "b", "c", "d", "e"]
        result = "0x"
        for _ in range(64):
            result += choice(chars)
        return result

    @classmethod
    def create(
        cls,
        src_session,
        dst_session,
        receiver: str,
        amount: int,
        sendingTokenName: str,
        receiveTokenName: str,
    ):
        """Create SoData class

        Args:
            receiver (str): The final recipient of the target token
            amount (int): Amount of tokens sent
            sendingTokenName (str): The name of the token sent on the source chain side, like usdt etc.
            receiveTokenName (str): The name of the token  to the target link, like usdt etc.

        Returns:
            SoData: SoData class
        """
        transactionId = cls.generate_random_bytes32()
        return SoData(
            transactionId=transactionId,
            receiver=receiver,
            sourceChainId=src_session.put_task(func=get_chain_id),
            sendingAssetId=src_session.put_task(
                func=get_token_address, args=(sendingTokenName,)
            ),
            destinationChainId=dst_session.put_task(func=get_chain_id),
            receivingAssetId=dst_session.put_task(
                func=get_token_address, args=(receiveTokenName,)
            ),
            amount=amount,
        )


class ConnextData(View):
    def __init__(
        self,
        dstDomain,
        dstSoDiamond,
        bridgeToken,
        slippage,
        isNative,
        relayFee,
        receiveLocal,
    ):
        self.dstDomain = dstDomain
        self.dstSoDiamond = dstSoDiamond
        self.bridgeToken = bridgeToken
        self.slippage = slippage
        self.isNative = isNative
        self.relayFee = relayFee
        self.receiveLocal = receiveLocal

    def format_to_contract(self):
        """Get the Bool data passed into the contract interface"""
        return [
            self.dstDomain,
            self.dstSoDiamond,
            self.bridgeToken,
            self.slippage,
            self.isNative,
            self.relayFee,
            self.receiveLocal,
        ]


class SwapType:
    """Interfaces that may be called"""

    IUniswapV2Router02 = "IUniswapV2Router02"
    IUniswapV2Router02AVAX = "IUniswapV2Router02AVAX"
    ISwapRouter = "ISwapRouter"


class SwapFunc:
    """Swap functions that may be called"""

    swapExactETHForTokens = "swapExactETHForTokens"
    swapExactAVAXForTokens = "swapExactAVAXForTokens"
    swapExactTokensForETH = "swapExactTokensForETH"
    swapExactTokensForAVAX = "swapExactTokensForAVAX"
    swapExactTokensForTokens = "swapExactTokensForTokens"
    exactInput = "exactInput"


class SwapData(View):
    """Constructing data for calling UniswapLike"""

    def __init__(
        self,
        callTo,
        approveTo,
        sendingAssetId,
        receivingAssetId,
        fromAmount,
        callData,
        swapType: str = None,
        swapFuncName: str = None,
        swapPath: list = None,
        swapEncodePath: list = None,
    ):
        # The swap address
        self.callTo = callTo
        # The swap address
        self.approveTo = approveTo
        # The swap start token address
        self.sendingAssetId = sendingAssetId
        # The swap final token address
        self.receivingAssetId = receivingAssetId
        # The swap start token amount
        self.fromAmount = fromAmount
        # The swap callData
        self.callData = callData
        self.swapType = swapType
        self.swapFuncName = swapFuncName
        self.swapPath = swapPath
        self.swapEncodePath = swapEncodePath

    def format_to_contract(self):
        """Returns the data used to pass into the contract interface"""
        return [
            self.callTo,
            self.approveTo,
            self.sendingAssetId,
            self.receivingAssetId,
            self.fromAmount,
            self.callData,
        ]

    @classmethod
    def create(
        cls,
        swapType: str,
        swapFuncName: str,
        fromAmount: int,
        swapPath: list,
        p: Project = None,
    ):
        """Create SwapData class

        Args:
            swapType (str): Calling the uniswap interface type
            swapFuncName (str): Calling a specific function name
            fromAmount (int): Input amount for Swap
            swapPath (list): Token path for Swap
            p (Project, optional): Load brownie project config. Defaults to None.

        Raises:
            ValueError: Not support swapFuncName

        Returns:
            swap_data: SwapData class
        """
        if swapFuncName not in vars(SwapFunc):
            raise ValueError("Not support")
        swap_info = None
        for v in get_swap_info():
            if swapType in v:
                swap_info = v[swapType]
        swap_contract = Contract.from_abi(
            swapType, swap_info["router"], getattr(p.interface, swapType).abi
        )
        callTo = swap_contract.address
        approveTo = swap_contract.address
        minAmount = 0

        if swapType == SwapType.ISwapRouter:
            path = cls.encode_path_for_uniswap_v3(swapPath)
            if swapFuncName == "exactInput":
                if swapPath[0] == "weth":
                    sendingAssetId = zero_address()
                else:
                    sendingAssetId = get_token_address(swapPath[0])
                receivingAssetId = get_token_address(swapPath[-1])
            else:
                raise ValueError("Not support")
        else:
            path = cls.encode_path_for_uniswap_v2(swapPath)
            sendingAssetId = path[0]
            receivingAssetId = path[-1]

        if swapFuncName in [
            SwapFunc.swapExactTokensForTokens,
        ]:
            callData = getattr(swap_contract, swapFuncName).encode_input(
                fromAmount,
                minAmount,
                path,
                "0x8d18dfcea276dc3f5d76270f9f544cb74b39c4eb",
                int(time.time() + 3000),
            )
        elif swapFuncName == SwapFunc.exactInput:
            callData = getattr(swap_contract, swapFuncName).encode_input(
                [
                    path,
                    p["SoDiamond"][-1].address,
                    int(time.time() + 3000),
                    fromAmount,
                    minAmount,
                ]
            )
        elif swapFuncName in [
            SwapFunc.swapExactETHForTokens,
            SwapFunc.swapExactAVAXForTokens,
        ]:
            callData = getattr(swap_contract, swapFuncName).encode_input(
                minAmount, path, p["SoDiamond"][-1].address, int(time.time() + 3000)
            )
        else:
            raise ValueError("Not support")
        swap_data = SwapData(
            callTo,
            approveTo,
            sendingAssetId,
            receivingAssetId,
            fromAmount,
            callData,
            swapType,
            swapFuncName,
            swapPath,
            path,
        )
        return swap_data

    @staticmethod
    def reset_min_amount(
        callData: str,
        swapType: str,
        swapFuncName: str,
        minAmount: int,
        p: Project = None,
    ):
        """Resetting the min amount of dst swap based on the results of the overall slippage calculation

        Args:
            callData (str): Calldata for target chain execution swap
            swapType (str): Calling the uniswap interface type
            swapFuncName (str): Calling a specific function name
            minAmount (int): Min amount
            p (Project, optional): Load brownie project config. Defaults to None.

        Raises:
            ValueError: not support swapType

        Returns:
            callData: Calldata after setting min amount
        """
        swap_info = None
        for v in get_swap_info():
            if swapType in v:
                swap_info = v[swapType]
        swap_contract = Contract.from_abi(
            swapType, swap_info["router"], getattr(p.interface, swapType).abi
        )
        if swapType == SwapType.ISwapRouter and swapFuncName == "exactInput":
            [params] = getattr(swap_contract, swapFuncName).decode_input(callData)
            params[4] = minAmount
            return getattr(swap_contract, swapFuncName).encode_input(params)
        elif swapType.startswith("IUniswapV2") and swapFuncName.startswith(
            "swapExactTokens"
        ):
            (fromAmount, _, path, to, deadline) = getattr(
                swap_contract, swapFuncName
            ).decode_input(callData)
            return getattr(swap_contract, swapFuncName).encode_input(
                fromAmount, minAmount, path, to, deadline
            )
        elif swapType.startswith("IUniswapV2") and (
            swapFuncName.startswith("swapExactETH")
            or swapFuncName.startswith("swapExactAVAX")
        ):
            (_, path, to, deadline) = getattr(swap_contract, swapFuncName).decode_input(
                callData
            )
            return getattr(swap_contract, swapFuncName).encode_input(
                minAmount, path, to, deadline
            )
        else:
            raise ValueError("Not support")

    @classmethod
    def encode_path_for_uniswap_v3_revert(cls, swapPath):
        return cls.encode_path_for_uniswap_v3(swapPath[::-1])

    @staticmethod
    def encode_path_for_uniswap_v2(p: list):
        return [get_token_address(v) for v in p]

    @staticmethod
    def encode_path_for_uniswap_v3(p: list):
        """
        :param p: [token, fee, token, fee, token...]
        :return:
        """
        assert len(p) > 0
        assert (len(p) - 3) % 2 == 0, "p length not right"
        p = [
            (
                padding_to_bytes(
                    web3.toHex(int(p[i] * uniswap_v3_fee_decimal)),
                    padding="left",
                    length=3,
                )
                if (i + 1) % 2 == 0
                else get_token_address(p[i])
            )
            for i in range(len(p))
        ]
        return combine_bytes(p)

    @classmethod
    def estimate_out(cls, amountIn: int, swapType: str, swapPath, p: Project = None):
        """Estimate uniswap final output amount

        Args:
            amountIn (int): swap input amount
            swapType (str): uniswap interface type
            swapPath (_type_): swap token path
            p (Project, optional): Load brownie project config. Defaults to None.

        Raises:
            ValueError: not support swapType

        Returns:
            amountOut: final output amount
        """
        account = get_account()
        swap_info = get_swap_info()[swapType]
        if swapType == "ISwapRouter":
            swap_contract = Contract.from_abi(
                "IQuoter", swap_info["quoter"], getattr(p.interface, "IQuoter").abi
            )
            amountOut = swap_contract.quoteExactInput.call(
                cls.encode_path_for_uniswap_v3(swapPath), amountIn, {"from": account}
            )
        elif swapType.startswith("IUniswapV2"):
            swap_contract = Contract.from_abi(
                swapType, swap_info["router"], getattr(p.interface, swapType).abi
            )
            amountOuts = swap_contract.getAmountsOut(
                amountIn, cls.encode_path_for_uniswap_v2(swapPath)
            )
            amountOut = amountOuts[-1]
        else:
            raise ValueError("Not support")
        print(
            f"  Swap estimate out: token {swapPath[0]}, amount {amountIn / get_token_decimal(swapPath[0])} "
            f"-> token {swapPath[-1]}, amount {amountOut / get_token_decimal(swapPath[-1])}"
        )
        return amountOut

    @classmethod
    def estimate_in(cls, amountOut: int, swapType: str, swapPath, p: Project = None):
        """Estimate uniswap input amount based on output amount

        Args:
            amountOut (int): uniswap output amount
            swapType (str): uniswap interface type
            swapPath (_type_): swap token path
            p (Project, optional): load brownie project config. Defaults to None.

        Raises:
            ValueError: not support swapType

        Returns:
            amountIn: input amount
        """
        account = get_account()
        swap_info = get_swap_info()[swapType]
        if swapType == "ISwapRouter":
            swap_contract = Contract.from_abi(
                "IQuoter", swap_info["quoter"], getattr(p.interface, "IQuoter").abi
            )
            amountIn = swap_contract.quoteExactOutput.call(
                cls.encode_path_for_uniswap_v3_revert(swapPath),
                amountOut,
                {"from": account},
            )
        elif swapType.startswith("IUniswapV2"):
            swap_contract = Contract.from_abi(
                swapType, swap_info["router"], getattr(p.interface, swapType).abi
            )
            amountIns = swap_contract.getAmountsIn(
                amountOut, cls.encode_path_for_uniswap_v2(swapPath)
            )
            amountIn = amountIns[0]
        else:
            raise ValueError("Not support")
        print(
            f"  Swap estimate in: token {swapPath[0]}, amount {amountIn / get_token_decimal(swapPath[0])} "
            f"<- token {swapPath[-1]}, amount {amountOut / get_token_decimal(swapPath[-1])}"
        )
        return amountIn


def estimate_dst_gas(so_data, bridge_token, dst_swap_data, p: Project = None):
    from brownie import network

    account = get_account()
    proxy_diamond = Contract.from_abi(
        "ConnextFacet",
        "0x8D18DFCeA276DC3f5d76270F9F544cb74b39c4Eb",
        p["ConnextFacet"].abi,
    )

    estimate_gas = proxy_diamond.xReceiveForGas.estimate_gas(
        so_data.format_to_contract(),
        bridge_token,
        [] if dst_swap_data is None else [dst_swap_data.format_to_contract()],
        {"from": account},
    )
    net = network.show_active()

    print("estimate_gas", estimate_gas)

    execute_gas = get_connext_execute_gas()
    executeL1_gas = get_connext_execute_l1_gas()
    gas_amount = estimate_gas + execute_gas
    chain_id = get_chain_id()

    gelato_url = f"https://relay.gelato.digital/oracles/{chain_id}/estimate?paymentToken=0x0000000000000000000000000000000000000000&gasLimit={gas_amount}&gasLimitL1={executeL1_gas}&isHighPriority=false"
    dst_relayer_fee = requests.get(gelato_url)
    print("dst_relayer_fee", dst_relayer_fee)
    return get_fee_value(
        int(dst_relayer_fee.json()["estimatedFee"]), get_network_token(net)
    )


def so_swap_via_connext(
    so_data, src_swap_data, connext_data, dst_swap_data, input_value, p: Project = None
):
    account = get_account()
    proxy_diamond = Contract.from_abi(
        "ConnextFacet", p["SoDiamond"][-1].address, p["ConnextFacet"].abi
    )

    proxy_diamond.soSwapViaConnext(
        so_data.format_to_contract(),
        [] if src_swap_data is None else [src_swap_data.format_to_contract()],
        connext_data.format_to_contract(),
        [] if dst_swap_data is None else [dst_swap_data.format_to_contract()],
        {"from": account, "value": int(input_value)},
    )


def cross_swap_via_connext(
    src_session,
    dst_session,
    inputAmount,
    sourceTokenName,
    sourceSwapType,
    sourceSwapFunc,
    sourceSwapPath,
    destinationTokenName,
    destinationSwapType,
    destinationSwapFunc,
    destinationSwapPath,
):
    print(
        f"{'-' * 100}\nSwap from: network {src_session.net}, token: {sourceTokenName}\n"
        f"{dst_session.net}, token: {destinationTokenName}"
    )
    src_diamond_address = "0x6b23875dD0D74c767c329C4A927B4408337B65d8"
    dst_diamond_address = "0x8D18DFCeA276DC3f5d76270F9F544cb74b39c4Eb"
    print(
        f"Source diamond address: {src_diamond_address}. Destination diamond address: {dst_diamond_address}"
    )

    so_data = SoData.create(
        src_session,
        dst_session,
        src_session.put_task(get_account_address),
        amount=inputAmount,
        sendingTokenName=sourceTokenName,
        receiveTokenName=destinationTokenName,
    )
    print("SoData\n", so_data)

    if sourceSwapType is not None:
        src_swap_data = src_session.put_task(
            SwapData.create,
            args=(sourceSwapType, sourceSwapFunc, inputAmount, sourceSwapPath),
            with_project=True,
        )
        print("SourceSwapData:\n", src_swap_data)
        if sourceTokenName != "eth":
            src_session.put_task(
                token_approve,
                args=(
                    sourceTokenName,
                    src_diamond_address,
                    inputAmount,
                ),
                with_project=True,
            )
            input_eth_amount = 0
        else:
            input_eth_amount = inputAmount
    else:
        src_swap_data = None

    if destinationSwapType is not None:
        dst_swap_data: SwapData = dst_session.put_task(
            SwapData.create,
            args=(
                destinationSwapType,
                destinationSwapFunc,
                inputAmount,
                destinationSwapPath,
            ),
            with_project=True,
        )
    else:
        dst_swap_data: SwapData = None

    # if dst_swap_data is not None:
    #     dst_swap_data.callData = dst_session.put_task(
    #         SwapData.reset_min_amount,
    #         args=(
    #             dst_swap_data.callData,
    #             dst_swap_data.swapType,
    #             dst_swap_data.swapFuncName,
    #             0,  # todo: slipage cal
    #         ),
    #         with_project=True,
    #     )
    #     print("DestinationSwapData:\n", dst_swap_data)

    dst_domain = dst_session.put_task(get_connext_domain_id)

    if src_swap_data is not None:
        src_bridge_token = src_swap_data.receivingAssetId
    else:
        if sourceTokenName == "eth":
            token_name = "weth"
        else:
            token_name = sourceTokenName
        src_bridge_token = src_session.put_task(get_token_address, args=(token_name,))

    if dst_swap_data is not None:
        dst_bridge_token = dst_swap_data.sendingAssetId
    else:
        dst_bridge_token = dst_session.put_task(
            get_token_address, args=(destinationTokenName,)
        )

    send_token = dst_bridge_token
    receive_token = dst_session.put_task(
        func=get_token_address, args=(destinationTokenName,)
    )

    router_address, swap_calldata = kyber_calldata(
        dst_session.net,
        dst_diamond_address,
        dst_diamond_address,
        send_token,
        receive_token,
        inputAmount,
    )

    dst_swap_data.callTo = router_address
    dst_swap_data.approveTo = router_address
    dst_swap_data.callData = swap_calldata

    print("DestinationSwapData:\n", dst_swap_data)

    dst_relay_fee = dst_session.put_task(
        estimate_dst_gas,
        args=(
            so_data,
            dst_bridge_token,
            dst_swap_data,
        ),
        with_project=True,
    )

    gas_relay_fee = get_fee_amount(dst_relay_fee, get_network_token(src_session.net))
    print("gas_relay_fee", gas_relay_fee)

    input_value = input_eth_amount + gas_relay_fee

    print(f"Input value: {input_value}")

    connext_data = ConnextData(
        dst_domain,
        dst_diamond_address,
        src_bridge_token,
        300,
        True,
        gas_relay_fee,
        False,
    )
    print(f"ConnextData: {connext_data.format_to_contract()}")

    # src_session.put_task(
    #     so_swap_via_connext,
    #     args=(
    #         so_data,
    #         src_swap_data,
    #         connext_data,
    #         dst_swap_data,
    #         input_value,
    #     ),
    #     with_project=True,
    # )


def main(src_net="optimism-main", dst_net="arbitrum-main"):
    global src_session
    global dst_session
    src_session = Session(
        net=src_net, project_path=root_path, name=src_net, daemon=False
    )
    dst_session = Session(
        net=dst_net, project_path=root_path, name=dst_net, daemon=False
    )

    # without swap
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=int(0.0001 * 1e18),
    #     sourceTokenName="eth",
    #     destinationTokenName="eth",
    #     sourceSwapType=None,
    #     sourceSwapFunc=None,
    #     sourceSwapPath=None,
    #     destinationSwapType=None,
    #     destinationSwapFunc=None,
    #     destinationSwapPath=None,
    # )

    # without swap but native
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=1e16,
    #     sourceTokenName="eth",
    #     destinationTokenName="eth",
    #     sourceSwapType=None,
    #     sourceSwapFunc=None,
    #     sourceSwapPath=None,
    #     destinationSwapType=None,
    #     destinationSwapFunc=None,
    #     destinationSwapPath=None,
    # )

    # only src swap
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=int(100 * 1e6),
    #     sourceTokenName="usdc",
    #     destinationTokenName="connext-test",
    #     sourceSwapType=SwapType.IUniswapV2Router02,
    #     sourceSwapFunc=SwapFunc.swapExactTokensForTokens,
    #     sourceSwapPath=("usdc", "connext-test"),
    #     destinationSwapType=None,
    #     destinationSwapFunc=None,
    #     destinationSwapPath=None,
    # )

    # only src swap with native
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=1e5,
    #     sourceTokenName="bool-usdt",
    #     destinationTokenName="eth",
    #     sourceSwapType=SwapType.IUniswapV2Router02,
    #     sourceSwapFunc=SwapFunc.swapExactTokensForETH,
    #     sourceSwapPath=('bool-usdt', 'weth'),
    #     destinationSwapType=None,
    #     destinationSwapFunc=None,
    #     destinationSwapPath=None,
    # )

    # only dst swap
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=int(100 * 1e18),
    #     sourceTokenName="connext-test",
    #     destinationTokenName="usdc",
    #     sourceSwapType=None,
    #     sourceSwapFunc=None,
    #     sourceSwapPath=None,
    #     destinationSwapType=SwapType.IUniswapV2Router02,
    #     destinationSwapFunc=SwapFunc.swapExactTokensForTokens,
    #     destinationSwapPath=("connext-test", "usdc"),
    # )

    # only dst swap with native
    # cross_swap_via_connext(
    #     src_session=src_session,
    #     dst_session=dst_session,
    #     inputAmount=1e15,
    #     sourceTokenName="eth",
    #     destinationTokenName="bool-usdt",
    #     sourceSwapType=None,
    #     sourceSwapFunc=None,
    #     sourceSwapPath=None,
    #     destinationSwapType=SwapType.IUniswapV2Router02,
    #     destinationSwapFunc=SwapFunc.swapExactETHForTokens,
    #     destinationSwapPath=('weth', 'bool-usdt'),
    # )

    # src and dst swap
    cross_swap_via_connext(
        src_session=src_session,
        dst_session=dst_session,
        inputAmount=int(0.1 * 1e6),
        sourceTokenName="usdt",
        destinationTokenName="usdc",
        sourceSwapType=None,
        sourceSwapFunc=None,
        sourceSwapPath=("usdt", "usdc"),
        destinationSwapType=SwapType.IUniswapV2Router02,
        destinationSwapFunc=SwapFunc.swapExactTokensForTokens,
        destinationSwapPath=("usdt", "usdc"),
    )

    src_session.terminate()
    dst_session.terminate()


if __name__ == "__main__":
    main()
